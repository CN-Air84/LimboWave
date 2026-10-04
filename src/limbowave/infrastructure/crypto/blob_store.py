"""加密 blob 仓：图片与剪贴板文档的字节内容存储（Phase 4 / 设计计划 §八）。

设计：
- 每个 blob 是一个独立文件 ``<blob_id>.bin``，内容为 ChaCha20-Poly1305 密文
  （与字段加密同一主密钥，逐 blob 随机 nonce）。
- blob_id 是内容哈希派生（``sha256`` 前 16 字节 hex）——同内容天然去重，
  且哈希本身就是完整性校验的一部分（AEAD 另有 tag 防篡改）。
- 磁盘上没有任何明文内容；blob 之间的关联只在数据库登记卡里。

崩溃安全：先写临时文件再原子替换，与 vault 同一模式。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from limbowave.infrastructure.crypto.vault import VaultKey

_NONCE_BYTES = 12


class BlobStoreError(Exception):
    """blob 仓错误的基类。"""


class BlobTampered(BlobStoreError):
    """blob 内容损坏或被篡改（AEAD tag 校验失败）。"""


class BlobStore:
    """加密 blob 仓。所有写入密文落盘，读出时解密并校验完整性。"""

    def __init__(self, directory: Path, key: VaultKey) -> None:
        self._dir = directory
        self._key = key

    def put(self, content: bytes) -> str:
        """存入内容，返回 blob_id（内容寻址）。同内容幂等。"""
        blob_id = hashlib.sha256(content).hexdigest()[:32]
        path = self._dir / f"{blob_id}.bin"
        if path.is_file():
            return blob_id  # 内容寻址：已存在即幂等
        nonce = secrets.token_bytes(_NONCE_BYTES)
        key_bytes = self._key_material()
        sealed = ChaCha20Poly1305(key_bytes).encrypt(nonce, content, None)
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f"{blob_id}.tmp"
        tmp.write_bytes(nonce + sealed)
        os.replace(tmp, path)
        return blob_id

    def get(self, blob_id: str) -> bytes:
        """读出并解密。不存在抛 KeyError，篡改抛 BlobTampered。"""
        path = self._dir / f"{blob_id}.bin"
        if not path.is_file():
            raise KeyError(f"blob 不存在：{blob_id}")
        raw = path.read_bytes()
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return ChaCha20Poly1305(self._key_material()).decrypt(nonce, sealed, None)
        except InvalidTag as exc:
            raise BlobTampered(f"blob 已损坏或被篡改：{blob_id}") from exc

    def exists(self, blob_id: str) -> bool:
        return (self._dir / f"{blob_id}.bin").is_file()

    def delete(self, blob_id: str) -> bool:
        """删除 blob。返回是否真的删了（幂等：不存在返回 False）。"""
        path = self._dir / f"{blob_id}.bin"
        if not path.is_file():
            return False
        path.unlink()
        return True

    # ---------- 内部 ----------

    def _key_material(self) -> bytes:
        """32 字节主密钥材料（与字段加密同一主密钥）。"""
        return self._key.key_bytes()
