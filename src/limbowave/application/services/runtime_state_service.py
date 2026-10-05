"""Runtime 状态服务：从持久化镜像构建可移植的 Runtime 快照。

这段构建逻辑原本只在集成测试里（test_recovery_e2e / test_persistence_recovery_e2e），
GUI 的「切换会话」同样需要它——切会话 = 从目标会话的镜像重建 Pi 上下文。
提升到应用层后，测试与 GUI 共用同一实现，避免两份代码漂移。

镜像去重与排序规则（与恢复验收一致）：
- 按 ``captured_at`` 升序遍历，同一 ``entry_id`` 只取第一次出现（后者是重复捕获）；
- Pi entry ID 只作溯源，快照的指纹按语义内容计算（``fingerprint_from_entry``）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.compression_context import without_application_compactions
from limbowave.domain.conversation import Conversation
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.domain.runtime_state import RuntimeStateSnapshot, fingerprint_from_entry


class RuntimeStateService:
    """从 Runtime 镜像仓库构建恢复/切换用快照。只读。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def build_snapshot(self, conversation_id: str) -> RuntimeStateSnapshot | None:
        """从持久化镜像重建一个会话的 Runtime 快照。无镜像返回 None。"""
        with self._uow_factory() as uow:
            mirrors = uow.runtime.list_for_conversation(conversation_id)
            if not mirrors:
                return None

            entries: list[dict[str, Any]] = []
            seen: set[str] = set()
            for mirror in sorted(mirrors, key=lambda m: (m.captured_at, m.id)):
                if mirror.entry_id in seen:
                    continue
                seen.add(mirror.entry_id)
                entry = dict(mirror.payload)
                entry.setdefault("id", mirror.entry_id)
                entry.setdefault("type", mirror.entry_type)
                if mirror.parent_entry_id:
                    entry.setdefault("parentId", mirror.parent_entry_id)
                entries.append(entry)

            entries = without_application_compactions(
                isolate_conversation_entries(entries, uow.conversations.get(conversation_id))
            )
            if not entries:
                return None
            fingerprints = [
                fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(entries)
            ]
            return RuntimeStateSnapshot(
                conversation_id=conversation_id,
                entries=entries,
                leaf_entry_id=entries[-1].get("id"),
                fingerprint=fingerprints,
            )

    def build_branch_snapshot(
        self, conversation_id: str, leaf_entry_id: str
    ) -> RuntimeStateSnapshot | None:
        """从镜像重建**到指定叶子为止**的快照（分支切换用）。

        沿 ``parent_entry_id`` 链从叶子回溯到根，只保留这条路径上的条目——
        分支切换的语义是「把 Runtime 定位到该分支的叶子」，其他分支的条目
        不应出现在恢复后的上下文里。叶子不存在返回 None。
        """
        with self._uow_factory() as uow:
            mirrors = uow.runtime.list_for_conversation(conversation_id)
            conversation = uow.conversations.get(conversation_id)
        return self.build_branch_snapshot_from_records(conversation, mirrors, leaf_entry_id)

    @staticmethod
    def build_branch_snapshot_from_records(
        conversation: Conversation | None,
        mirrors: list[RuntimeEntryMirror],
        leaf_entry_id: str,
    ) -> RuntimeStateSnapshot | None:
        """从调用方已读取的镜像构建分支快照，避免切换时重复解密同批记录。"""
        if not mirrors:
            return None

        by_entry: dict[str, RuntimeEntryMirror] = {}
        for mirror in sorted(mirrors, key=lambda m: (m.captured_at, m.id)):
            by_entry.setdefault(mirror.entry_id, mirror)  # 去重：取首次捕获

        if leaf_entry_id not in by_entry:
            return None

        # 回溯到根
        chain: list[RuntimeEntryMirror] = []
        cursor: str | None = leaf_entry_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor in visited:
                raise ValueError("运行时镜像的父链存在循环，无法安全恢复")
            visited.add(cursor)
            current = by_entry.get(cursor)
            if current is None:
                break
            chain.append(current)
            cursor = current.parent_entry_id
        chain.reverse()

        entries: list[dict[str, Any]] = []
        for mirror in chain:
            entry = dict(mirror.payload)
            entry.setdefault("id", mirror.entry_id)
            entry.setdefault("type", mirror.entry_type)
            if mirror.parent_entry_id:
                entry.setdefault("parentId", mirror.parent_entry_id)
            entries.append(entry)

        entries = without_application_compactions(
            isolate_conversation_entries(entries, conversation)
        )
        if not entries:
            return None
        fingerprints = [fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(entries)]
        return RuntimeStateSnapshot(
            conversation_id=conversation.id if conversation is not None else "",
            entries=entries,
            leaf_entry_id=entries[-1].get("id"),
            fingerprint=fingerprints,
        )


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    value = entry.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def isolate_conversation_entries(
    entries: list[dict[str, Any]], conversation: Conversation | None
) -> list[dict[str, Any]]:
    """隔离旧版串线留下的会话前缀，只改恢复副本，不删除审计数据。

    仅在发现早于会话创建时间的用户消息时启用。Pi 时间精度为毫秒，允许这一
    精度差；不能用 message_id 作边界，旧版曾把同文的旧消息错误关联到新消息。
    确认污染后只保留本会话用户消息的后继，不恢复可能总结了旧会话的摘要。
    无法确认归属的前缀宁可不恢复，也不能冒充当前会话上下文。
    """
    if conversation is None:
        return entries
    boundary = conversation.created_at - timedelta(milliseconds=1)
    users = [e for e in entries if (e.get("message") or {}).get("role") == "user"]
    if not any((time := _entry_time(e)) is not None and time < boundary for e in users):
        return entries

    roots = {
        str(e["id"]) for e in users
        if (time := _entry_time(e)) is not None and time >= boundary
    }
    if not roots:
        return []
    by_id = {str(e["id"]): e for e in entries}
    kept: dict[str, dict[str, Any]] = {}
    for entry in entries:
        # 这些是 Pi 恢复所需的配置，不进入对话文本；删除会让 Pi 自动补条目，破坏指纹。
        if entry.get("type") in {"model_change", "thinking_level_change"}:
            kept[str(entry["id"])] = dict(entry)
            continue
        # 压缩/分支摘要可能在新会话里生成，却引用被污染的祖先；恢复原始消息而非摘要。
        if entry.get("type") in {"compaction", "branch_summary"}:
            continue
        cursor: str | None = str(entry["id"])
        visited: set[str] = set()
        while cursor in by_id and cursor not in visited:
            if cursor in roots:
                kept[str(entry["id"])] = dict(entry)
                break
            visited.add(cursor)
            cursor = by_id[cursor].get("parentId")

    # 被移除的旧祖先/摘要不得作为新根的 parentId；其余合法父子链与工具消息保留。
    for entry in kept.values():
        parent = entry.get("parentId")
        visited = set()
        while parent is not None and parent not in kept:
            if parent in visited:
                raise ValueError("运行时镜像的父链存在循环，无法安全恢复")
            visited.add(parent)
            parent = by_id.get(parent, {}).get("parentId")
        entry["parentId"] = parent
    return list(kept.values())
