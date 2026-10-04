"""应用权威数据的仓库端口（Protocol）。

设计约束（Phase 1B 验收）：

- 端口是**同步**的。Python 的 ``sqlite3`` 本身就是同步 API，将来落 SQLite 时
  ``commit()`` / ``rollback()`` 的语义可以直接对上，不需要重写调用方。
- ``UnitOfWork`` 提供**真事务语义**：写入先暂存，``commit()`` 一次性生效，
  ``rollback()`` 全部丢弃。内存实现也严格执行这一点，因此原子性现在就可测。
- 端口只描述"要什么"，不规定存储形态；内存实现与将来的 SQLite 实现都满足同一组不变量。

原子性要求（必须由实现保证）：

1. 用户消息、``RunRecord`` 与请求意图快照**原子创建**；
2. 最终助手消息、``TransportSnapshot`` 与 Runtime 镜像在 settled 后**一致提交**；
3. Runtime 崩溃时本轮能落成 ``interrupted``；
4. 重复收到终止事件**幂等**（不产生第二条助手消息或重复快照）。
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol

from limbowave.domain.compaction import CompressionVersion
from limbowave.domain.conversation import Branch, Conversation, Message
from limbowave.domain.files import FileDocument, ImageAttachment
from limbowave.domain.memory import MemoryDocument
from limbowave.domain.permissions import PermissionAudit, PermissionGrant
from limbowave.domain.run import RunLogSummary, RunRecord
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot


class ConversationRepository(Protocol):
    def add(self, conversation: Conversation) -> None: ...

    def update(self, conversation: Conversation) -> None: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list_all(self) -> list[Conversation]: ...

    def delete(self, conversation_id: str) -> None:
        """删除会话及其全部下级数据（分支/消息/运行/快照/镜像，级联）。"""
        ...


class BranchRepository(Protocol):
    def add(self, branch: Branch) -> None: ...

    def update(self, branch: Branch) -> None: ...

    def get(self, branch_id: str) -> Branch | None: ...

    def list_for_conversation(self, conversation_id: str) -> list[Branch]: ...

    def delete(self, branch_id: str) -> None: ...


class MessageRepository(Protocol):
    def add(self, message: Message) -> None: ...

    def update(self, message: Message) -> None: ...

    def get(self, message_id: str) -> Message | None: ...

    def list_for_branch(self, branch_id: str) -> list[Message]: ...

    def count_for_branch(self, branch_id: str) -> int:
        """只取数量，不为列表/侧栏解密消息正文。"""
        ...


class RunRepository(Protocol):
    def add(self, run: RunRecord) -> None: ...

    def update(self, run: RunRecord) -> None: ...

    def get(self, run_id: str) -> RunRecord | None: ...

    def list_for_conversation(self, conversation_id: str) -> list[RunRecord]: ...

    def list_log_summaries(
        self, conversation_id: str, *, limit: int, offset: int,
    ) -> list[RunLogSummary]: ...

    def list_running(self) -> list[RunRecord]: ...


class SnapshotRepository(Protocol):
    """请求双快照。意图快照每轮一条；传输快照每轮可有多条（工具调用会多次往返）。"""

    def add_intent(self, snapshot: RequestIntentSnapshot) -> None: ...

    def add_transport(self, snapshot: TransportSnapshot) -> None: ...

    def get_intent(self, run_id: str) -> RequestIntentSnapshot | None: ...

    def list_transport(self, run_id: str) -> list[TransportSnapshot]: ...

    def list_all_intents(self) -> list[RequestIntentSnapshot]:
        """全部意图快照。用于附件引用检查（删除文档/图片前的在引用判定）。"""
        ...


class RuntimeMirrorRepository(Protocol):
    def add_many(self, mirrors: list[RuntimeEntryMirror]) -> None: ...

    def list_for_conversation(self, conversation_id: str) -> list[RuntimeEntryMirror]: ...

    def list_for_run(self, run_id: str) -> list[RuntimeEntryMirror]: ...

    def link_message(self, mirror_id: str, message_id: str) -> None:
        """给孤儿镜像补消息关联。只允许从空到非空，**绝不改绑**。"""
        ...


class FileDocumentRepository(Protocol):
    """文件文档登记卡（TXT/MD/剪贴板）。内容本体在加密 blob 仓，这里只有元数据。"""

    def add(self, document: FileDocument) -> None: ...

    def update(self, document: FileDocument) -> None: ...

    def get(self, document_id: str) -> FileDocument | None: ...

    def list_all(self) -> list[FileDocument]: ...

    def delete(self, document_id: str) -> None: ...


class ImageAttachmentRepository(Protocol):
    """图片附件登记卡。原图字节在加密 blob 仓。"""

    def add(self, attachment: ImageAttachment) -> None: ...

    def get(self, attachment_id: str) -> ImageAttachment | None: ...

    def list_all(self) -> list[ImageAttachment]: ...

    def delete(self, attachment_id: str) -> None: ...


class PermissionRepository(Protocol):
    """会话级权限授权 + 权限审计（§11.1/§11.3）。"""

    def add_grant(self, grant: PermissionGrant) -> None: ...

    def list_grants(self, conversation_id: str) -> list[PermissionGrant]: ...

    def get_grant(self, grant_id: str) -> PermissionGrant | None: ...

    def delete_grant(self, grant_id: str) -> None:
        """撤销或缩小授权（§11.1：用户可随时撤销或缩小）。"""
        ...

    def add_audit(self, record: PermissionAudit) -> None: ...

    def list_audit(self, conversation_id: str) -> list[PermissionAudit]: ...


class CompressionVersionRepository(Protocol):
    """压缩版本（§7.4）。同一分支同一时刻至多一个启用版本。"""

    def add(self, version: CompressionVersion) -> None: ...

    def update(self, version: CompressionVersion) -> None: ...

    def get(self, version_id: str) -> CompressionVersion | None: ...

    def list_for_branch(self, branch_id: str) -> list[CompressionVersion]: ...

    def get_active(self, branch_id: str) -> CompressionVersion | None: ...

    def set_active(self, branch_id: str, version_id: str) -> None:
        """把某版本设为启用（同时取消该分支其他版本的启用标记）。"""
        ...

    def clear_active(self, branch_id: str) -> None:
        """取消该分支的启用版本（回退到未压缩状态）。"""
        ...


class MemoryRepository(Protocol):
    def get(self, document_id: str) -> MemoryDocument | None: ...

    def put(self, document: MemoryDocument) -> None: ...


class UnitOfWork(Protocol):
    """一次事务的工作单元。可作上下文管理器使用。"""

    conversations: ConversationRepository
    branches: BranchRepository
    messages: MessageRepository
    runs: RunRepository
    snapshots: SnapshotRepository
    runtime: RuntimeMirrorRepository
    file_documents: FileDocumentRepository
    image_attachments: ImageAttachmentRepository
    compressions: CompressionVersionRepository
    permissions: PermissionRepository
    memories: MemoryRepository

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


# 调用方（RunCoordinator）依赖的工厂签名
UnitOfWorkFactory = Callable[[], UnitOfWork]
