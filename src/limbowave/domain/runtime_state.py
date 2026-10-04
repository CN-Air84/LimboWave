"""Runtime 恢复契约：内核无关的可移植快照、恢复结果与校验结果。

设计原则（Phase 1C）：

- 应用层不知道 Pi 的 JSONL 格式、``switch_session`` 命令或临时文件路径。
  它只构造 ``RuntimeStateSnapshot``，交给 ``AgentKernel`` 实现。
- 恢复后必须重新读取 Pi entries 并做**语义校验**，不能只看 RPC 返回 success。
- 语义指纹不含密钥、认证头、非确定性时间戳——只有可语义比较的结构化内容。
- 恢复完成只意味着"会话重新可用"，**不自动重发**被中断的用户消息。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class RecoveryState(enum.StrEnum):
    """恢复状态机。"""

    AVAILABLE = "available"
    LOST = "lost"
    RESTARTING = "restarting"
    RESTORING = "restoring"
    VALIDATING = "validating"
    RECOVERED = "recovered"
    RECOVERY_FAILED = "recovery_failed"


class RestoreFailureReason(enum.StrEnum):
    """恢复失败的结构化原因。适配器返回这个，由应用层决定 UI 状态。"""

    NO_SNAPSHOT = "no_snapshot"
    MATERIALIZATION_FAILED = "materialization_failed"
    SESSION_SWITCH_FAILED = "session_switch_failed"
    ENTRY_VALIDATION_FAILED = "entry_validation_failed"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    RUNTIME_EXITED_DURING_RESTORE = "runtime_exited_during_restore"


@dataclass(frozen=True, slots=True)
class EntryFingerprint:
    """单个条目的语义指纹。

    基于可语义比较的内容计算，不含密钥、认证头或非确定性时间戳。
    恢复后 Pi 可能分配新的 entry ID，因此指纹不包含 entry ID——
    只比较类型、角色、内容摘要与父子位置。
    """

    entry_type: str
    role: str | None
    content_hash: str
    parent_position: int | None
    branch_position: int

    def matches(self, other: EntryFingerprint) -> bool:
        """语义等价：只比类型、角色与内容摘要。

        **不比 parent_position / branch_position**——恢复后 Pi 可能分配不同的 entry ID，
        导致父引用位置变化。内容才是语义稳定点，位置是布局细节。
        """
        return (
            self.entry_type == other.entry_type
            and self.role == other.role
            and self.content_hash == other.content_hash
        )


@dataclass(frozen=True, slots=True)
class FingerprintSet:
    """一组条目指纹的集合比较结果。"""

    expected: list[EntryFingerprint]
    actual: list[EntryFingerprint]

    @property
    def count_matches(self) -> bool:
        return len(self.expected) == len(self.actual)

    @property
    def all_match(self) -> bool:
        if not self.count_matches:
            return False
        return all(a.matches(b) for a, b in zip(self.expected, self.actual, strict=True))

    @staticmethod
    def _key(f: EntryFingerprint) -> tuple[str, str | None, str, int | None, int]:
        return (f.entry_type, f.role, f.content_hash, f.parent_position, f.branch_position)

    @property
    def missing(self) -> list[EntryFingerprint]:
        """期望中有但实际缺失的指纹。"""
        actual_keys = {self._key(f) for f in self.actual}
        return [f for f in self.expected if self._key(f) not in actual_keys]

    @property
    def extra(self) -> list[EntryFingerprint]:
        """实际中多出的指纹。"""
        expected_keys = {self._key(f) for f in self.expected}
        return [f for f in self.actual if self._key(f) not in expected_keys]


@dataclass(frozen=True, slots=True)
class RuntimeStateSnapshot:
    """应用理解的可移植快照。

    由 ``RuntimeEntryMirror`` 列表构造。Pi 的具体格式（JSONL v3）由适配器在物化时处理——
    应用层不关心也不应该知道。
    """

    conversation_id: str
    entries: list[dict[str, Any]]
    leaf_entry_id: str | None
    fingerprint: list[EntryFingerprint]
    source_runtime_instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeRestoreResult:
    """恢复尝试的结果。"""

    success: bool
    failure_reason: RestoreFailureReason | None = None
    restored_entry_count: int = 0
    actual_fingerprint: list[EntryFingerprint] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeValidationResult:
    """恢复后语义校验的结果。"""

    valid: bool
    fingerprint_comparison: FingerprintSet | None = None
    has_system_entry: bool = False
    last_stable_entry_id: str | None = None
    error: str | None = None


def fingerprint_from_entry(entry: dict[str, Any], branch_position: int) -> EntryFingerprint:
    """从 Pi entry 或 RuntimeEntryMirror 计算语义指纹。

    内容哈希基于规范化文本——不计较空白差异，但角色与类型必须精确匹配。
    """
    import hashlib

    entry_type = str(entry.get("type", "unknown"))
    message = entry.get("message") or {}
    role = message.get("role") if isinstance(message, dict) else None

    content = ""
    if isinstance(message, dict):
        raw_content = message.get("content")
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            parts: list[str] = []
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            content = "".join(parts)
        # 非 message 类型的 entry 用 payload 摘要
        if not content and "payload" in entry:
            content = str(entry.get("payload", ""))

    normalized = content.strip()
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    parent_entry_id = entry.get("parentId") or entry.get("parent_entry_id")
    parent_position: int | None = None
    if parent_entry_id is not None:
        # 父位置在调用方构造快照时确定；这里只标 None 让调用方填
        parent_position = None

    return EntryFingerprint(
        entry_type=entry_type,
        role=role if role else "",
        content_hash=content_hash,
        parent_position=parent_position,
        branch_position=branch_position,
    )
