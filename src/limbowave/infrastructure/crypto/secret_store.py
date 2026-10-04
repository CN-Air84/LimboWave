"""加密密钥库：以引用名存取密钥，落盘为密文。

设计约束（Phase 1A 验收 + ADR-0002）：
- 密钥**绝不**以明文落盘，绝不进入普通配置、Pi 的 ``auth.json``、日志或异常文本。
- 加密密钥来自 :class:`~limbowave.infrastructure.crypto.vault.VaultKey`——即资料库主密钥，
  主密钥本身只以「主密码派生 KEK 包裹」的密文形式落盘。磁盘上没有可用的明文密钥。

**旧格式迁移**：Phase 1A 的实现把随机主密钥明文存在 ``master.key`` 里。首次用资料库
解锁后调用 :func:`migrate_legacy_secrets`，会把旧密文逐个解密、用资料库主密钥重新加密，
然后删除 ``master.key``。
"""

from __future__ import annotations

import base64
import contextlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from limbowave.infrastructure.crypto.vault import VaultKey

_LEGACY_KEY_FILE = "master.key"
_DATA_FILE = "secrets.json"
_NONCE_BYTES = 12


class SecretStore:
    """加密密钥库。加解密委托给资料库主密钥（``VaultKey``）。"""

    def __init__(self, key: VaultKey, data_path: Path) -> None:
        self._key = key
        self._data_path = data_path

    # ---------- 读写 ----------

    def _read_all(self) -> dict[str, str]:
        if not self._data_path.is_file():
            return {}
        data: dict[str, str] = json.loads(self._data_path.read_text(encoding="utf-8"))
        return data

    def _write_all(self, data: dict[str, str]) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        self._data_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with contextlib.suppress(OSError):
            self._data_path.chmod(0o600)

    # ---------- 公开 API ----------

    def set(self, ref: str, secret: str) -> None:
        data = self._read_all()
        data[ref] = self._key.encrypt(secret)
        self._write_all(data)

    def get(self, ref: str) -> str | None:
        blob = self._read_all().get(ref)
        if blob is None:
            return None
        return self._key.decrypt(blob)

    def delete(self, ref: str) -> None:
        data = self._read_all()
        if ref in data:
            del data[ref]
            self._write_all(data)

    def refs(self) -> list[str]:
        return sorted(self._read_all().keys())


def migrate_legacy_secrets(directory: Path, key: VaultKey) -> int:
    """把 Phase 1A 旧格式（明文 ``master.key`` 加密）的密钥迁入资料库主密钥体系。

    返回迁移的条目数。无旧格式时返回 0。迁移成功后删除 ``master.key``。
    任一条目解密失败则整体中止并抛出——宁可保留旧文件也不写半截数据。
    """
    key_path = directory / _LEGACY_KEY_FILE
    data_path = directory / _DATA_FILE
    if not key_path.is_file() or not data_path.is_file():
        return 0

    raw = base64.b64decode(key_path.read_text(encoding="utf-8").strip())
    if len(raw) != 32:
        raise RuntimeError("旧密钥库主密钥损坏")
    legacy = ChaCha20Poly1305(raw)

    data: dict[str, str] = json.loads(data_path.read_text(encoding="utf-8"))
    migrated: dict[str, str] = {}
    for ref, blob in data.items():
        sealed = base64.b64decode(blob)
        plaintext = legacy.decrypt(sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], None)
        migrated[ref] = key.encrypt(plaintext.decode("utf-8"))

    store = SecretStore(key, data_path)
    store._write_all(migrated)
    key_path.unlink()
    return len(migrated)
