"""凭据服务：把 ``credential_ref`` 解析为实际密钥。

密钥本体只存在于加密密钥库；本服务在使用时解密，且**不缓存、不写入日志、不进异常文本**
（Phase 1A 验收：密钥不得出现在普通配置、auth.json、日志或异常中）。
"""

from __future__ import annotations

from typing import Protocol


class SecretStore(Protocol):
    """密钥库抽象。infrastructure 提供加密实现。"""

    def get(self, ref: str) -> str | None: ...

    def set(self, ref: str, secret: str) -> None: ...

    def delete(self, ref: str) -> None: ...

    def refs(self) -> list[str]: ...


class CredentialService:
    """按引用解析密钥。"""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def resolve(self, ref: str | None) -> str | None:
        """解析密钥引用。无引用或不存在返回 None。绝不抛含密钥的异常。"""
        if ref is None:
            return None
        return self._store.get(ref)

    def store_secret(self, ref: str, secret: str) -> None:
        self._store.set(ref, secret)

    def list_refs(self) -> list[str]:
        return self._store.refs()
