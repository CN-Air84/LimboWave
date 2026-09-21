"""加密密钥库：以引用名存取密钥，落盘为密文。

设计约束（Phase 1A 验收）：
- 密钥**绝不**以明文落盘，绝不进入普通配置、Pi 的 ``auth.json``、日志或异常文本。
- 本实现用 XChaCha20-Poly1305 语义（``cryptography`` 的 ``ChaCha20Poly1305``）加密每个值。
- 主密钥为随机 32 字节，存于 ``master.key``（与应用数据同目录，用户私有）。
  **已知局限**：主密钥与密文同盘，能读写该目录者即可解密。Windows Hello / 主密码
  派生 KEK 属后续硬化（设计计划 §12.3 的 vault_key_provider 接缝），本阶段不实现。
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

_KEY_FILE = "master.key"
_DATA_FILE = "secrets.json"
_NONCE_BYTES = 12


class SecretStore:
    """加密密钥库。值以密文落盘，密钥只在使用时存在于内存。"""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._data_path = directory / _DATA_FILE
        self._key_path = directory / _KEY_FILE
        self._key: bytes | None = None

    # ---------- 主密钥 ----------

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._key_path.is_file():
            raw = base64.b64decode(self._key_path.read_text(encoding="utf-8").strip())
        else:
            raw = secrets.token_bytes(32)
            self._key_path.write_text(base64.b64encode(raw).decode("ascii"), encoding="utf-8")
            self._restrict(self._key_path)
        if len(raw) != 32:
            raise RuntimeError("密钥库主密钥损坏")
        self._key = raw
        return raw

    @staticmethod
    def _restrict(path: Path) -> None:
        """尽力收窄权限（POSIX 上 0600；Windows 依赖用户目录的私有性）。"""
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    # ---------- 读写 ----------

    def _read_all(self) -> dict[str, str]:
        if not self._data_path.is_file():
            return {}
        data: dict[str, str] = json.loads(self._data_path.read_text(encoding="utf-8"))
        return data

    def _write_all(self, data: dict[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._data_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._restrict(self._data_path)

    def _encrypt(self, plaintext: str) -> str:
        key = self._load_key()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        cipher = ChaCha20Poly1305(key)
        sealed = cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + sealed).decode("ascii")

    def _decrypt(self, blob: str) -> str:
        key = self._load_key()
        raw = base64.b64decode(blob)
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        cipher = ChaCha20Poly1305(key)
        return cipher.decrypt(nonce, sealed, None).decode("utf-8")

    # ---------- 公开 API ----------

    def set(self, ref: str, secret: str) -> None:
        data = self._read_all()
        data[ref] = self._encrypt(secret)
        self._write_all(data)

    def get(self, ref: str) -> str | None:
        blob = self._read_all().get(ref)
        if blob is None:
            return None
        return self._decrypt(blob)

    def delete(self, ref: str) -> None:
        data = self._read_all()
        if ref in data:
            del data[ref]
            self._write_all(data)

    def refs(self) -> list[str]:
        return sorted(self._read_all().keys())
