"""资料库信封加密：主密码 → Argon2id → KEK → 包裹主密钥。

设计依据：docs/architecture/adr-0002-vault-encryption.md、设计计划 §12.2。

链路：

    主密码 → Argon2id(salt) → KEK(32B) → 包裹主密钥 → wrapped_master_key（落盘）
    资料库主密钥(32B, 随机) → 加密敏感字段（逐值随机 nonce）

关键性质：

- 主密钥**只以密文形式**落盘。磁盘上没有任何可用的明文密钥。
- 改主密码只**重新封装**主密钥，不重写数据。
- KDF 参数记录在 vault 文件里，将来调参不会破坏既有资料库。
- ``wraps`` 是列表结构，为 Phase 1.3 的 Windows Hello 第二份包装预留。

**如实记录的限制**：解锁期间主密钥存在于进程内存，Python 无法可靠擦除，``lock()``
只能丢弃引用。这是所有纯用户态加密方案的共同限制。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from limbowave.infrastructure.crypto.recovery import (
    METHOD_DPAPI,
    HelloGate,
    RecoveryError,
    hello_gate,
)

VAULT_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16

# Argon2id 参数：面向桌面交互式登录的合理默认。
# 记录进 vault 文件，因此调参不会破坏既有资料库。
DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST = 65536  # 64 MiB
DEFAULT_PARALLELISM = 4


class VaultError(Exception):
    """资料库加密的基类错误。"""


class InvalidPassword(VaultError):
    """主密码错误（或 vault 文件被篡改）。"""


class VaultLocked(VaultError):
    """尚未解锁，无法访问主密钥。"""


class VaultExists(VaultError):
    """已存在资料库，拒绝覆盖。"""


@dataclass(frozen=True, slots=True)
class KdfParams:
    """Argon2id 参数。随 vault 文件持久化。"""

    time_cost: int = DEFAULT_TIME_COST
    memory_cost: int = DEFAULT_MEMORY_COST
    parallelism: int = DEFAULT_PARALLELISM

    def to_json(self) -> dict[str, int]:
        return {
            "time_cost": self.time_cost,
            "memory_cost": self.memory_cost,
            "parallelism": self.parallelism,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KdfParams:
        return cls(
            time_cost=int(data.get("time_cost", DEFAULT_TIME_COST)),
            memory_cost=int(data.get("memory_cost", DEFAULT_MEMORY_COST)),
            parallelism=int(data.get("parallelism", DEFAULT_PARALLELISM)),
        )


@dataclass(frozen=True, slots=True)
class KeyWrap:
    """一份主密钥包装。``method`` 区分主密码包装与将来的系统保护包装。"""

    method: str
    blob: str
    salt: str | None = None
    kdf: KdfParams | None = None
    created_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"method": self.method, "blob": self.blob}
        if self.salt is not None:
            out["salt"] = self.salt
        if self.kdf is not None:
            out["kdf"] = self.kdf.to_json()
        if self.created_at is not None:
            out["created_at"] = self.created_at
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KeyWrap:
        return cls(
            method=str(data["method"]),
            blob=str(data["blob"]),
            salt=data.get("salt"),
            kdf=KdfParams.from_json(data["kdf"]) if data.get("kdf") else None,
            created_at=data.get("created_at"),
        )


def derive_kek(password: str, salt: bytes, params: KdfParams) -> bytes:
    """Argon2id 派生密钥加密密钥。"""
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def wrap_key(kek: bytes, master_key: bytes) -> str:
    """用 KEK 包裹主密钥，返回 base64(nonce + ciphertext)。"""
    nonce = secrets.token_bytes(NONCE_BYTES)
    sealed = ChaCha20Poly1305(kek).encrypt(nonce, master_key, None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def unwrap_key(kek: bytes, blob: str) -> bytes:
    """解开主密钥。密码错误或密文被篡改 → InvalidPassword。"""
    try:
        raw = base64.b64decode(blob)
        nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        return ChaCha20Poly1305(kek).decrypt(nonce, sealed, None)
    except (InvalidTag, ValueError) as exc:
        raise InvalidPassword("主密码错误或资料库已损坏") from exc


class VaultKey:
    """已解锁的资料库主密钥。用于加解密敏感字段。"""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("主密钥长度必须是 32 字节")
        self._key = key

    def key_bytes(self) -> bytes:
        """原始密钥材料。仅供同为加密边界的模块（如 blob 仓）做字节级加密。

        **红线**：不得写入日志、异常、配置或任何落盘位置。
        """
        return self._key

    def encrypt(self, plaintext: str) -> str:
        """加密一个字符串，返回 base64(nonce + ciphertext)。"""
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = ChaCha20Poly1305(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, blob: str) -> str:
        """解密。密文被篡改 → VaultError。"""
        try:
            raw = base64.b64decode(blob)
            nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
            return ChaCha20Poly1305(self._key).decrypt(nonce, sealed, None).decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise VaultError("密文已损坏或被篡改") from exc


class Vault:
    """资料库加密边界。管理 vault 文件的创建、解锁与改密码。"""

    def __init__(self, path: Path, *, params: KdfParams | None = None) -> None:
        self._path = path
        self._params = params or KdfParams()
        self._key: VaultKey | None = None

    # ---------- 状态 ----------

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def require_key(self) -> VaultKey:
        if self._key is None:
            raise VaultLocked("资料库尚未解锁")
        return self._key

    # ---------- 创建与解锁 ----------

    def create(self, password: str) -> VaultKey:
        """创建资料库：生成随机主密钥并用主密码派生的 KEK 包裹。"""
        if self.exists:
            raise VaultExists(f"资料库已存在：{self._path}")
        if not password:
            raise VaultError("主密码不得为空")

        master_key = secrets.token_bytes(KEY_BYTES)
        salt = secrets.token_bytes(SALT_BYTES)
        kek = derive_kek(password, salt, self._params)
        wrap = KeyWrap(
            method="password",
            blob=wrap_key(kek, master_key),
            salt=_b64(salt),
            kdf=self._params,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._write([wrap])
        self._key = VaultKey(master_key)
        return self._key

    def unlock(self, password: str) -> VaultKey:
        """用主密码解锁。"""
        data = self._read()
        master_key: bytes | None = None
        for wrap in data["wraps"]:
            if wrap.method != "password" or wrap.salt is None or wrap.kdf is None:
                continue
            kek = derive_kek(password, _unb64(wrap.salt), wrap.kdf)
            master_key = unwrap_key(kek, wrap.blob)  # 密码错 → InvalidPassword
            break
        if master_key is None:
            raise VaultError("资料库中没有可用的主密码包装")
        self._key = VaultKey(master_key)
        return self._key

    def change_password(self, old_password: str, new_password: str) -> None:
        """改主密码：**只重新封装主密钥**，不重写任何数据（§12.2）。"""
        self.unlock(old_password)
        master_key = self.require_key()
        if not new_password:
            raise VaultError("新主密码不得为空")

        # 取回原主密钥字节以重新包裹
        raw_master = _extract_master_key(self, master_key, old_password)

        salt = secrets.token_bytes(SALT_BYTES)
        kek = derive_kek(new_password, salt, self._params)
        new_wrap = KeyWrap(
            method="password",
            blob=wrap_key(kek, raw_master),
            salt=_b64(salt),
            kdf=self._params,
            created_at=datetime.now(UTC).isoformat(),
        )
        # 保留非主密码的包装（如将来的 Windows Hello）
        data = self._read()
        others = [w for w in data["wraps"] if w.method != "password"]
        self._write([new_wrap, *others])

    # ---------- 系统保护恢复（Task 1.3 / §12.3） ----------

    def recovery_enabled(self) -> bool:
        """是否已配置系统保护恢复封装。"""
        if not self.exists:
            return False
        return any(w.method == METHOD_DPAPI for w in self._read()["wraps"])

    def enable_recovery(self, password: str, *, gate: HelloGate | None = None) -> str:
        """启用系统保护：把主密钥再用 Windows 账户保护一份。

        需要先用主密码解锁（防止他人趁人离开时悄悄启用恢复）。
        返回当前保护等级的说明文本（供界面**如实**展示）。
        """
        self.unlock(password)
        raw_master = _extract_master_key(self, self.require_key(), password)

        from limbowave.infrastructure.crypto.recovery import dpapi_protect, encode_blob

        wrap = KeyWrap(
            method=METHOD_DPAPI,
            blob=encode_blob(dpapi_protect(raw_master)),
            created_at=datetime.now(UTC).isoformat(),
        )
        data = self._read()
        others = [w for w in data["wraps"] if w.method != METHOD_DPAPI]
        self._write([*others, wrap])

        from limbowave.infrastructure.crypto.recovery import describe_protection

        return describe_protection()

    def disable_recovery(self) -> bool:
        """关闭系统保护（§1.3「支持主动关闭恢复」）。返回是否确实移除了封装。"""
        if not self.exists:
            return False
        data = self._read()
        remaining = [w for w in data["wraps"] if w.method != METHOD_DPAPI]
        if len(remaining) == len(data["wraps"]):
            return False  # 本来就没开
        if not remaining:
            raise VaultError("关闭恢复会留下一个无法解锁的资料库，已拒绝")
        self._write(remaining)
        return True

    def unlock_with_recovery(self, new_password: str, *, gate: HelloGate | None = None) -> VaultKey:
        """用系统保护重置主密码：**不需要旧主密码**。

        顺序固定：Hello 验证（若有）→ 取回主密钥 → 用新密码重新封装。
        验证不通过或取不回密钥时不改任何东西。
        """
        from limbowave.infrastructure.crypto.recovery import (
            decode_blob,
            dpapi_unprotect,
        )

        data = self._read()
        wrap = next((w for w in data["wraps"] if w.method == METHOD_DPAPI), None)
        if wrap is None:
            raise VaultError("未启用系统保护恢复，无法重置主密码")

        active_gate = gate if gate is not None else hello_gate()
        if not active_gate.verify("重置 LimboWave 资料库主密码"):
            raise RecoveryError("身份验证未通过，已取消（资料库未改动）")

        raw_master = dpapi_unprotect(decode_blob(wrap.blob))
        if len(raw_master) != KEY_BYTES:
            raise VaultError("系统保护取回的密钥长度异常，已拒绝")

        if not new_password:
            raise VaultError("新主密码不得为空")
        salt = secrets.token_bytes(SALT_BYTES)
        kek = derive_kek(new_password, salt, self._params)
        new_wrap = KeyWrap(
            method="password",
            blob=wrap_key(kek, raw_master),
            salt=_b64(salt),
            kdf=self._params,
            created_at=datetime.now(UTC).isoformat(),
        )
        # 保留系统保护封装，密码封装换成新的
        others = [w for w in data["wraps"] if w.method != "password"]
        self._write([new_wrap, *others])
        self._key = VaultKey(raw_master)
        return self._key

    def lock(self) -> None:
        """丢弃主密钥引用。

        **限制**：Python 无法可靠擦除内存，这不等于密钥已从进程内存中消失。
        """
        self._key = None

    # ---------- 文件读写 ----------

    def _read(self) -> dict[str, Any]:
        if not self.exists:
            raise VaultError(f"资料库不存在：{self._path}")
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != VAULT_VERSION:
            raise VaultError(f"不支持的资料库版本：{data.get('version')}")
        return {"version": VAULT_VERSION, "wraps": [KeyWrap.from_json(w) for w in data["wraps"]]}

    def _write(self, wraps: list[KeyWrap]) -> None:
        payload = {
            "version": VAULT_VERSION,
            "wraps": [w.to_json() for w in wraps],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：崩溃不会留下半截 vault
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)


def _extract_master_key(vault: Vault, key: VaultKey, password: str) -> bytes:
    """从已解锁的 vault 取回主密钥原始字节（改密码时重新封装用）。"""
    data = vault._read()
    for wrap in data["wraps"]:
        if wrap.method == "password" and wrap.salt and wrap.kdf:
            kek = derive_kek(password, _unb64(wrap.salt), wrap.kdf)
            return unwrap_key(kek, wrap.blob)
    raise VaultError("找不到主密码包装")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)
