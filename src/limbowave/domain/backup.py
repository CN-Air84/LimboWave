"""备份包格式与完整性模型（Phase 8 / 设计计划 §十四.3）。

**加密备份包**是一份自包含文件：

    ┌─ 明文头（可读，用于恢复前展示信息）───────────────┐
    │ magic "LWBK" · 格式版本 · KDF 参数 · salt · nonce   │
    ├─ 密文（ChaCha20-Poly1305，密钥来自**备份密码**）────┤
    │ tar 归档：manifest.json + limbowave.db + blobs/*   │
    │           + config.json                            │
    └────────────────────────────────────────────────────┘

两个关键性质：

1. **独立备份密码**：密钥由用户提供的备份密码经 Argon2id 派生，**不使用资料库
   主密钥、不依赖 Windows Hello**——这样备份才能迁移到新设备（§十四.3 明确要求）。
2. **完整性两层**：manifest 里每个文件有 sha256（逐文件校验），
   外层 AEAD tag 覆盖整个归档（整体防篡改）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

MAGIC = b"LWBK"
FORMAT_VERSION = 1


class BackupError(Exception):
    """备份/恢复失败的基类。"""


class WrongPassword(BackupError):
    """备份密码错误，或归档被篡改（AEAD tag 校验失败）。"""


class IntegrityError(BackupError):
    """完整性校验失败（某个文件的哈希与清单不符）。"""


class IncompatibleBackup(BackupError):
    """备份的 schema 版本高于当前应用支持的版本——拒绝恢复。"""


class BackupKind(enum.StrEnum):
    """恢复动作的种类（用于恢复前展示与决策）。"""

    FRESH = "fresh"  # 目标为空，可直接恢复
    OVERWRITE = "overwrite"  # 目标已有资料库，恢复会覆盖


@dataclass(frozen=True, slots=True)
class EntryHash:
    """清单里一个文件的哈希与大小。"""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """备份清单（§十四.3：版本清单和完整性哈希）。"""

    format_version: int
    schema_version: int  # 数据库 schema 版本——恢复时做兼容性检查
    app_version: str
    created_at: datetime
    entries: tuple[EntryHash, ...] = ()
    # 内容统计（恢复前展示）
    conversation_count: int = 0
    message_count: int = 0
    blob_count: int = 0
    grant_count: int = 0

    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)


@dataclass(frozen=True, slots=True)
class BackupInspection:
    """恢复前的检查结果（§十四.3：验证包完整性 → 显示版本和内容统计 → 检查兼容性）。"""

    manifest: BackupManifest
    integrity_ok: bool
    compatible: bool
    integrity_problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_restore(self) -> bool:
        return self.integrity_ok and self.compatible

    def summary(self) -> str:
        """给用户看的一行摘要。"""
        m = self.manifest
        parts = [
            f"格式 v{m.format_version}",
            f"schema v{m.schema_version}",
            f"应用 {m.app_version}",
            f"{m.created_at.astimezone():%Y-%m-%d %H:%M}",
            f"{m.conversation_count} 会话 / {m.message_count} 消息",
            f"{m.blob_count} 个附件对象",
            f"{m.grant_count} 条权限授权",
            f"{m.total_bytes() / 1024:.1f} KB",
        ]
        return " · ".join(parts)


def check_compatibility(manifest: BackupManifest, current_schema_version: int) -> None:
    """兼容性检查：备份的 schema 版本**高于**当前应用时拒绝恢复。

    低版本备份可以恢复（迁移系统会把 schema 升上来）；高版本不行——
    我们不知道新 schema 的含义，硬恢复会损坏数据。
    """
    if manifest.format_version > FORMAT_VERSION:
        raise IncompatibleBackup(
            f"备份格式 v{manifest.format_version} 高于本应用支持的 v{FORMAT_VERSION}"
        )
    if manifest.schema_version > current_schema_version:
        raise IncompatibleBackup(
            f"备份来自更新的应用（schema v{manifest.schema_version} > "
            f"当前 v{current_schema_version}），请先升级应用"
        )
