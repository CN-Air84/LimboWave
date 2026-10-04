"""内存仓库实现 + 工作单元。

刻意实现**真事务语义**而不是简单写穿字典：

- 写入先进入暂存区；``commit()`` 一次性落到 backing store；``rollback()`` 全部丢弃。
- 事务内可读到自己刚写入的数据（读路径先查暂存、再查 backing store）。

这样"用户消息 + RunRecord + 意图快照原子创建"与"助手消息 + 传输快照 + Runtime 镜像
一致提交"这两条不变量**现在就能被测试**，而不是等到接了 SQLite 才发现语义不对。
将来换 SQLite 时，同一组调用序列对应一个真实事务，调用方无需改动。
"""

from __future__ import annotations

from dataclasses import replace
from types import TracebackType
from typing import Any

from limbowave.domain.compaction import CompressionVersion
from limbowave.domain.conversation import Branch, Conversation, Message
from limbowave.domain.files import FileDocument, ImageAttachment
from limbowave.domain.memory import MemoryDocument
from limbowave.domain.permissions import PermissionAudit, PermissionGrant
from limbowave.domain.run import RunLogSummary, RunRecord, RunStatus
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot


class InMemoryStore:
    """backing store：只有 ``commit()`` 才会被写入。"""

    def __init__(self) -> None:
        self.memories: dict[str, MemoryDocument] = {}
        self.conversations: dict[str, Conversation] = {}
        self.branches: dict[str, Branch] = {}
        self.messages: dict[str, Message] = {}
        self.runs: dict[str, RunRecord] = {}
        self.intents: dict[str, RequestIntentSnapshot] = {}
        self.transports: dict[str, TransportSnapshot] = {}
        self.mirrors: dict[str, RuntimeEntryMirror] = {}
        self.file_documents: dict[str, FileDocument] = {}
        self.image_attachments: dict[str, ImageAttachment] = {}
        self.compressions: dict[str, CompressionVersion] = {}
        # 每个分支当前启用的压缩版本 id（同一分支至多一个）
        self.active_compression: dict[str, str] = {}
        self.permission_grants: dict[str, PermissionGrant] = {}
        self.permission_audit: dict[str, PermissionAudit] = {}


class _Staged[T]:
    """某个实体类型的暂存区：按 id 覆盖写。"""

    def __init__(self) -> None:
        self._items: dict[str, T] = {}

    def put(self, key: str, value: T) -> None:
        self._items[key] = value

    def get(self, key: str) -> T | None:
        return self._items.get(key)

    def values(self) -> list[T]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()


class _Conversations:
    def __init__(self, store: InMemoryStore, staged: _Staged[Conversation]) -> None:
        self._store = store
        self._staged = staged
        self.pending_deletes: set[str] = set()

    def add(self, conversation: Conversation) -> None:
        self._staged.put(conversation.id, conversation)

    def update(self, conversation: Conversation) -> None:
        self._staged.put(conversation.id, conversation)

    def get(self, conversation_id: str) -> Conversation | None:
        if conversation_id in self.pending_deletes:
            return None
        return self._staged.get(conversation_id) or self._store.conversations.get(conversation_id)

    def list_all(self) -> list[Conversation]:
        merged = dict(self._store.conversations)
        merged.update({c.id: c for c in self._staged.values()})
        return [c for cid, c in merged.items() if cid not in self.pending_deletes]

    def delete(self, conversation_id: str) -> None:
        """标记删除。级联在 commit 时执行（与 SQLite 的 ON DELETE CASCADE 对齐）。"""
        self.pending_deletes.add(conversation_id)


class _Branches:
    def __init__(self, store: InMemoryStore, staged: _Staged[Branch]) -> None:
        self._store = store
        self._staged = staged
        self.pending_deletes: set[str] = set()

    def add(self, branch: Branch) -> None:
        self._staged.put(branch.id, branch)

    def update(self, branch: Branch) -> None:
        self._staged.put(branch.id, branch)

    def get(self, branch_id: str) -> Branch | None:
        if branch_id in self.pending_deletes:
            return None
        return self._staged.get(branch_id) or self._store.branches.get(branch_id)

    def list_for_conversation(self, conversation_id: str) -> list[Branch]:
        merged = dict(self._store.branches)
        merged.update({b.id: b for b in self._staged.values()})
        return [
            b
            for b in merged.values()
            if b.conversation_id == conversation_id and b.id not in self.pending_deletes
        ]

    def delete(self, branch_id: str) -> None:
        self.pending_deletes.add(branch_id)


class _Messages:
    def __init__(self, store: InMemoryStore, staged: _Staged[Message]) -> None:
        self._store = store
        self._staged = staged

    def add(self, message: Message) -> None:
        self._staged.put(message.id, message)

    def update(self, message: Message) -> None:
        self._staged.put(message.id, message)

    def get(self, message_id: str) -> Message | None:
        return self._staged.get(message_id) or self._store.messages.get(message_id)

    def list_for_branch(self, branch_id: str) -> list[Message]:
        merged = dict(self._store.messages)
        merged.update({m.id: m for m in self._staged.values()})
        rows = [m for m in merged.values() if m.branch_id == branch_id]
        return sorted(rows, key=lambda m: (m.created_at, m.id))

    def count_for_branch(self, branch_id: str) -> int:
        return len(self.list_for_branch(branch_id))


class _Runs:
    def __init__(
        self, store: InMemoryStore, staged: _Staged[RunRecord],
        staged_transports: _Staged[TransportSnapshot],
    ) -> None:
        self._store = store
        self._staged = staged
        self._staged_transports = staged_transports

    def add(self, run: RunRecord) -> None:
        self._staged.put(run.id, run)

    def update(self, run: RunRecord) -> None:
        self._staged.put(run.id, run)

    def get(self, run_id: str) -> RunRecord | None:
        return self._staged.get(run_id) or self._store.runs.get(run_id)

    def list_for_conversation(self, conversation_id: str) -> list[RunRecord]:
        merged = dict(self._store.runs)
        merged.update({r.id: r for r in self._staged.values()})
        rows = [r for r in merged.values() if r.conversation_id == conversation_id]
        return sorted(rows, key=lambda r: (r.created_at, r.id))

    def list_log_summaries(
        self, conversation_id: str, *, limit: int, offset: int,
    ) -> list[RunLogSummary]:
        runs = self.list_for_conversation(conversation_id)[offset:offset + limit]
        transports = dict(self._store.transports)
        transports.update({s.id: s for s in self._staged_transports.values()})
        counts = dict.fromkeys((r.id for r in runs), 0)
        for snapshot in transports.values():
            if snapshot.run_id in counts:
                counts[snapshot.run_id] += 1
        return [RunLogSummary(r.id, r.status, r.created_at, counts[r.id]) for r in runs]

    def list_running(self) -> list[RunRecord]:
        merged = dict(self._store.runs)
        merged.update({r.id: r for r in self._staged.values()})
        rows = [r for r in merged.values() if r.status is RunStatus.RUNNING]
        return sorted(rows, key=lambda r: (r.created_at, r.id))


class _Snapshots:
    def __init__(
        self,
        store: InMemoryStore,
        staged_intents: _Staged[RequestIntentSnapshot],
        staged_transports: _Staged[TransportSnapshot],
    ) -> None:
        self._store = store
        self._intents = staged_intents
        self._transports = staged_transports

    def add_intent(self, snapshot: RequestIntentSnapshot) -> None:
        self._intents.put(snapshot.id, snapshot)

    def add_transport(self, snapshot: TransportSnapshot) -> None:
        self._transports.put(snapshot.id, snapshot)

    def get_intent(self, run_id: str) -> RequestIntentSnapshot | None:
        merged = dict(self._store.intents)
        merged.update({s.id: s for s in self._intents.values()})
        for snapshot in merged.values():
            if snapshot.run_id == run_id:
                return snapshot
        return None

    def list_all_intents(self) -> list[RequestIntentSnapshot]:
        merged = dict(self._store.intents)
        merged.update({s.id: s for s in self._intents.values()})
        return sorted(merged.values(), key=lambda s: (s.created_at, s.id))

    def list_transport(self, run_id: str) -> list[TransportSnapshot]:
        merged = dict(self._store.transports)
        merged.update({s.id: s for s in self._transports.values()})
        rows = [s for s in merged.values() if s.run_id == run_id]
        return sorted(rows, key=lambda s: s.sequence)


class _RuntimeMirror:
    def __init__(self, store: InMemoryStore, staged: _Staged[RuntimeEntryMirror]) -> None:
        self._store = store
        self._staged = staged

    def add_many(self, mirrors: list[RuntimeEntryMirror]) -> None:
        for mirror in mirrors:
            self._staged.put(mirror.id, mirror)

    def _merged(self) -> list[RuntimeEntryMirror]:
        merged = dict(self._store.mirrors)
        merged.update({m.id: m for m in self._staged.values()})
        return list(merged.values())

    def list_for_conversation(self, conversation_id: str) -> list[RuntimeEntryMirror]:
        return [m for m in self._merged() if m.conversation_id == conversation_id]

    def list_for_run(self, run_id: str) -> list[RuntimeEntryMirror]:
        return [m for m in self._merged() if m.run_id == run_id]

    def link_message(self, mirror_id: str, message_id: str) -> None:
        current = self._staged.get(mirror_id) or self._store.mirrors.get(mirror_id)
        if current is None or current.message_id is not None:
            raise LookupError(f"运行时镜像不存在或已有关联，无法补链：{mirror_id}")
        self._staged.put(mirror_id, replace(current, message_id=message_id))


class _FileDocuments:
    def __init__(self, store: InMemoryStore, staged: _Staged[FileDocument]) -> None:
        self._store = store
        self._staged = staged
        self.pending_deletes: set[str] = set()

    def add(self, document: FileDocument) -> None:
        self._staged.put(document.id, document)

    def update(self, document: FileDocument) -> None:
        self._staged.put(document.id, document)

    def get(self, document_id: str) -> FileDocument | None:
        if document_id in self.pending_deletes:
            return None
        return self._staged.get(document_id) or self._store.file_documents.get(document_id)

    def list_all(self) -> list[FileDocument]:
        merged = dict(self._store.file_documents)
        merged.update({d.id: d for d in self._staged.values()})
        return [d for did, d in merged.items() if did not in self.pending_deletes]

    def delete(self, document_id: str) -> None:
        self.pending_deletes.add(document_id)


class _ImageAttachments:
    def __init__(self, store: InMemoryStore, staged: _Staged[ImageAttachment]) -> None:
        self._store = store
        self._staged = staged
        self.pending_deletes: set[str] = set()

    def add(self, attachment: ImageAttachment) -> None:
        self._staged.put(attachment.id, attachment)

    def get(self, attachment_id: str) -> ImageAttachment | None:
        if attachment_id in self.pending_deletes:
            return None
        return self._staged.get(attachment_id) or self._store.image_attachments.get(attachment_id)

    def list_all(self) -> list[ImageAttachment]:
        merged = dict(self._store.image_attachments)
        merged.update({a.id: a for a in self._staged.values()})
        return [a for aid, a in merged.items() if aid not in self.pending_deletes]

    def delete(self, attachment_id: str) -> None:
        self.pending_deletes.add(attachment_id)


class _Compressions:
    """压缩版本。启用标记也走暂存——rollback 必须能丢掉它（事务语义一致）。

    暂存的启用变更：``_staged_active`` 映射 branch_id → version_id（None 表示清除）。
    commit 时统一落到 ``store.active_compression``。
    """

    def __init__(self, store: InMemoryStore, staged: _Staged[CompressionVersion]) -> None:
        self._store = store
        self._staged = staged
        self._staged_active: dict[str, str | None] = {}

    def add(self, version: CompressionVersion) -> None:
        self._staged.put(version.id, version)

    def update(self, version: CompressionVersion) -> None:
        self._staged.put(version.id, version)

    def get(self, version_id: str) -> CompressionVersion | None:
        return self._staged.get(version_id) or self._store.compressions.get(version_id)

    def list_for_branch(self, branch_id: str) -> list[CompressionVersion]:
        merged = dict(self._store.compressions)
        merged.update({v.id: v for v in self._staged.values()})
        rows = [v for v in merged.values() if v.branch_id == branch_id]
        return sorted(rows, key=lambda v: (v.created_at, v.id))

    def get_active(self, branch_id: str) -> CompressionVersion | None:
        # 事务内的启用变更优先于 backing store
        if branch_id in self._staged_active:
            staged_id = self._staged_active[branch_id]
            return self.get(staged_id) if staged_id else None
        active_id = self._store.active_compression.get(branch_id)
        return self.get(active_id) if active_id else None

    def set_active(self, branch_id: str, version_id: str) -> None:
        self._staged_active[branch_id] = version_id

    def clear_active(self, branch_id: str) -> None:
        self._staged_active[branch_id] = None

    def apply_active(self) -> None:
        """commit 时调用：把暂存的启用变更落到 backing store。"""
        for branch_id, version_id in self._staged_active.items():
            if version_id is None:
                self._store.active_compression.pop(branch_id, None)
            else:
                self._store.active_compression[branch_id] = version_id
        self._staged_active.clear()

    def clear_staged(self) -> None:
        """rollback 时调用：丢弃暂存的启用变更。"""
        self._staged_active.clear()


class _Permissions:
    """权限授权（可删除）+ 权限审计（只追加）。"""

    def __init__(
        self,
        store: InMemoryStore,
        staged_grants: _Staged[PermissionGrant],
        staged_audit: _Staged[PermissionAudit],
    ) -> None:
        self._store = store
        self._grants = staged_grants
        self._audit = staged_audit
        self.pending_deletes: set[str] = set()

    # ----- 授权 -----

    def add_grant(self, grant: PermissionGrant) -> None:
        self._grants.put(grant.id, grant)

    def list_grants(self, conversation_id: str) -> list[PermissionGrant]:
        merged = dict(self._store.permission_grants)
        merged.update({g.id: g for g in self._grants.values()})
        rows = [
            g
            for gid, g in merged.items()
            if g.conversation_id == conversation_id and gid not in self.pending_deletes
        ]
        return sorted(rows, key=lambda g: (g.created_at, g.id))

    def get_grant(self, grant_id: str) -> PermissionGrant | None:
        if grant_id in self.pending_deletes:
            return None
        return self._grants.get(grant_id) or self._store.permission_grants.get(grant_id)

    def delete_grant(self, grant_id: str) -> None:
        self.pending_deletes.add(grant_id)

    # ----- 审计 -----

    def add_audit(self, record: PermissionAudit) -> None:
        self._audit.put(record.id, record)

    def list_audit(self, conversation_id: str) -> list[PermissionAudit]:
        merged = dict(self._store.permission_audit)
        merged.update({a.id: a for a in self._audit.values()})
        rows = [a for a in merged.values() if a.conversation_id == conversation_id]
        return sorted(rows, key=lambda a: (a.created_at, a.id))


class _Memories:
    def __init__(self, store: InMemoryStore, staged: _Staged[MemoryDocument]) -> None:
        self._store = store
        self._staged = staged

    def get(self, document_id: str) -> MemoryDocument | None:
        return self._staged.get(document_id) or self._store.memories.get(document_id)

    def put(self, document: MemoryDocument) -> None:
        self._staged.put(document.id, document)


class InMemoryUnitOfWork:
    """一个事务。未 ``commit()`` 的写入在退出时全部丢弃。"""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self._staged_memories: _Staged[MemoryDocument] = _Staged()
        self.memories = _Memories(store, self._staged_memories)
        self._staged_conversations: _Staged[Conversation] = _Staged()
        self._staged_branches: _Staged[Branch] = _Staged()
        self._staged_messages: _Staged[Message] = _Staged()
        self._staged_runs: _Staged[RunRecord] = _Staged()
        self._staged_intents: _Staged[RequestIntentSnapshot] = _Staged()
        self._staged_transports: _Staged[TransportSnapshot] = _Staged()
        self._staged_mirrors: _Staged[RuntimeEntryMirror] = _Staged()
        self._staged_file_documents: _Staged[FileDocument] = _Staged()
        self._staged_image_attachments: _Staged[ImageAttachment] = _Staged()
        self._staged_compressions: _Staged[CompressionVersion] = _Staged()
        self._staged_grants: _Staged[PermissionGrant] = _Staged()
        self._staged_audit: _Staged[PermissionAudit] = _Staged()
        self._committed = False

        self.conversations = _Conversations(store, self._staged_conversations)
        self.branches = _Branches(store, self._staged_branches)
        self.messages = _Messages(store, self._staged_messages)
        self.runs = _Runs(store, self._staged_runs, self._staged_transports)
        self.snapshots = _Snapshots(store, self._staged_intents, self._staged_transports)
        self.runtime = _RuntimeMirror(store, self._staged_mirrors)
        self.file_documents = _FileDocuments(store, self._staged_file_documents)
        self.image_attachments = _ImageAttachments(store, self._staged_image_attachments)
        self.compressions = _Compressions(store, self._staged_compressions)
        self.permissions = _Permissions(store, self._staged_grants, self._staged_audit)

    def _all_staged(self) -> list[tuple[dict[str, Any], _Staged[Any]]]:
        return [
            (self._store.memories, self._staged_memories),
            (self._store.conversations, self._staged_conversations),
            (self._store.branches, self._staged_branches),
            (self._store.messages, self._staged_messages),
            (self._store.runs, self._staged_runs),
            (self._store.intents, self._staged_intents),
            (self._store.transports, self._staged_transports),
            (self._store.mirrors, self._staged_mirrors),
            (self._store.file_documents, self._staged_file_documents),
            (self._store.image_attachments, self._staged_image_attachments),
            (self._store.compressions, self._staged_compressions),
            (self._store.permission_grants, self._staged_grants),
            (self._store.permission_audit, self._staged_audit),
        ]

    def commit(self) -> None:
        for target, staged in self._all_staged():
            for item in staged.values():
                target[item.id] = item
        self._apply_cascade_deletes()
        for key, doc in list(self._store.memories.items()):
            if (doc.conversation_id and doc.conversation_id not in self._store.conversations) or (
                doc.branch_id and doc.branch_id not in self._store.branches
            ):
                del self._store.memories[key]
        self.compressions.apply_active()  # 压缩启用标记随事务落
        self._committed = True
        self._clear()

    def _apply_cascade_deletes(self) -> None:
        """级联删除会话的全部下级数据（对齐 SQLite 的 ON DELETE CASCADE）。"""
        for conversation_id in self.conversations.pending_deletes:
            self._store.conversations.pop(conversation_id, None)
            branch_ids = {
                bid
                for bid, b in self._store.branches.items()
                if b.conversation_id == conversation_id
            }
            run_ids = {
                rid for rid, r in self._store.runs.items() if r.conversation_id == conversation_id
            }
            self._store.branches = {
                k: v for k, v in self._store.branches.items() if k not in branch_ids
            }
            self._store.messages = {
                k: v
                for k, v in self._store.messages.items()
                if v.conversation_id != conversation_id
            }
            self._store.runs = {k: v for k, v in self._store.runs.items() if k not in run_ids}
            self._store.intents = {
                k: v for k, v in self._store.intents.items() if v.run_id not in run_ids
            }
            self._store.transports = {
                k: v for k, v in self._store.transports.items() if v.run_id not in run_ids
            }
            self._store.mirrors = {
                k: v for k, v in self._store.mirrors.items() if v.conversation_id != conversation_id
            }
            # 压缩版本随会话级联（对齐 SQLite ON DELETE CASCADE）
            removed_branches = branch_ids
            self._store.compressions = {
                k: v
                for k, v in self._store.compressions.items()
                if v.conversation_id != conversation_id
            }
            self._store.active_compression = {
                k: v for k, v in self._store.active_compression.items() if k not in removed_branches
            }
        for branch_id in self.branches.pending_deletes:
            self._store.branches.pop(branch_id, None)
            message_ids = {
                mid
                for mid, message in self._store.messages.items()
                if message.branch_id == branch_id
            }
            run_ids = {
                rid for rid, run in self._store.runs.items() if run.branch_id == branch_id
            }
            self._store.messages = {
                mid: message
                for mid, message in self._store.messages.items()
                if mid not in message_ids
            }
            self._store.runs = {
                rid: run for rid, run in self._store.runs.items() if rid not in run_ids
            }
            self._store.intents = {
                key: value
                for key, value in self._store.intents.items()
                if value.run_id not in run_ids
            }
            self._store.transports = {
                key: value
                for key, value in self._store.transports.items()
                if value.run_id not in run_ids
            }
            self._store.mirrors = {
                key: replace(
                    mirror,
                    run_id=None if mirror.run_id in run_ids else mirror.run_id,
                    message_id=None if mirror.message_id in message_ids else mirror.message_id,
                )
                for key, mirror in self._store.mirrors.items()
            }
            self._store.compressions = {
                key: value
                for key, value in self._store.compressions.items()
                if value.branch_id != branch_id
            }
            self._store.active_compression.pop(branch_id, None)
        # 文件文档、图片附件、权限授权的独立删除（无级联——它们是顶层实体）
        for document_id in self.file_documents.pending_deletes:
            self._store.file_documents.pop(document_id, None)
        for attachment_id in self.image_attachments.pending_deletes:
            self._store.image_attachments.pop(attachment_id, None)
        for grant_id in self.permissions.pending_deletes:
            self._store.permission_grants.pop(grant_id, None)

    def rollback(self) -> None:
        self._clear()

    def _clear(self) -> None:
        for _, staged in self._all_staged():
            staged.clear()
        self.conversations.pending_deletes.clear()
        self.branches.pending_deletes.clear()
        self.file_documents.pending_deletes.clear()
        self.image_attachments.pending_deletes.clear()
        self.compressions.clear_staged()
        self.permissions.pending_deletes.clear()

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        if not self._committed:
            self.rollback()
        return None


def in_memory_uow_factory(store: InMemoryStore | None = None) -> Any:
    """产出一个 ``() -> UnitOfWork`` 工厂，共享同一个 backing store。"""
    backing = store if store is not None else InMemoryStore()

    def _factory() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(backing)

    return _factory
