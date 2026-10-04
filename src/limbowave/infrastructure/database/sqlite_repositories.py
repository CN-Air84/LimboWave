"""SQLite 仓库实现：持久化 + 字段级加密 + 真事务。

设计依据：设计计划 Task 1.1 / §12.1、ADR-0002。

关键性质：

- **字段级加密**（ADR-0002）：消息正文、标题、运行错误、请求体与头、镜像 payload
  以密文存储；id、时间戳、角色、状态等元数据明文。
- **真事务**：所有写入在同一个 ``sqlite3`` 连接的事务里，``commit()`` 前不可见，
  ``rollback()`` 全部丢弃。直接复用端口在 Phase 1B 定下的语义，调用方无需改动。
- **外键启用**：每次开连接就 ``PRAGMA foreign_keys = ON``，
  否则 SQLite 会静默忽略 REFERENCES 约束。
- 加密在**边界**完成：领域对象进出仓库时明文，落库前才加密。领域层不知道加密存在。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from limbowave.application.repositories import (
    BranchRepository,
    CompressionVersionRepository,
    ConversationRepository,
    FileDocumentRepository,
    ImageAttachmentRepository,
    MemoryRepository,
    MessageRepository,
    PermissionRepository,
    RunRepository,
    RuntimeMirrorRepository,
    SnapshotRepository,
)
from limbowave.domain.compaction import CompressionStatus, CompressionVersion
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole, MessageStatus
from limbowave.domain.files import FileDocument, FileKind, ImageAttachment, ImageFormat
from limbowave.domain.permissions import (
    Capability,
    Decision,
    PermissionAudit,
    PermissionGrant,
    PermissionPreset,
    RiskLevel,
)
from limbowave.domain.run import RunLogSummary, RunRecord, RunStatus
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot
from limbowave.domain.tool_step import ToolStep
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.memory_repository import SqliteMemoryRepository
from limbowave.infrastructure.database.migrations import migrate


def _dump_tool_steps(steps: tuple[ToolStep, ...]) -> str:
    """工具步骤序列化为 JSON（再由字段加密落库）。"""
    return json.dumps([s.to_json() for s in steps], ensure_ascii=False)


def _load_tool_steps(blob: str) -> tuple[ToolStep, ...]:
    if not blob:
        return ()
    try:
        return tuple(ToolStep.from_json(item) for item in json.loads(blob))
    except (ValueError, TypeError, KeyError):
        return ()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


class SqliteUnitOfWork:
    """一次数据库事务。

    持有一个连接与一个已解锁的 ``VaultKey``。所有读写走同一连接，
    因此事务语义与 SQLite 原生语义一致。
    """

    def __init__(
        self, conn: sqlite3.Connection, key: VaultKey, *, close_on_exit: bool = False,
        on_close: Callable[[SqliteUnitOfWork], None] | None = None,
    ) -> None:
        self._conn = conn
        self._key = key
        self._close_on_exit = close_on_exit
        self._on_close = on_close
        self._committed = False
        self._closed = False

        # 属性注解为端口协议类型：协议成员属性按不变性检查，
        # 具体实现类型直接赋值会被 mypy 拒绝
        self.conversations: ConversationRepository = _Conversations(conn, key)
        self.branches: BranchRepository = _Branches(conn, key)
        self.messages: MessageRepository = _Messages(conn, key)
        self.runs: RunRepository = _Runs(conn, key)
        self.snapshots: SnapshotRepository = _Snapshots(conn, key)
        self.runtime: RuntimeMirrorRepository = _RuntimeMirror(conn, key)
        self.file_documents: FileDocumentRepository = _FileDocuments(conn, key)
        self.image_attachments: ImageAttachmentRepository = _ImageAttachments(conn, key)
        self.compressions: CompressionVersionRepository = _Compressions(conn, key)
        self.permissions: PermissionRepository = _Permissions(conn, key)
        self.memories: MemoryRepository = SqliteMemoryRepository(conn, key)

    # ---------- 事务 ----------

    def commit(self) -> None:
        self._conn.commit()
        self._committed = True

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True
            if self._on_close is not None:
                self._on_close(self)
                self._on_close = None

    def __enter__(self) -> SqliteUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        # 工厂拥有的独立连接必须随事务关闭，不能等待 GC 才释放 Windows 文件句柄。
        try:
            if not self._committed and not self._closed:
                self.rollback()
        finally:
            if self._close_on_exit:
                self.close()
        return None


class _Conversations:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, conversation: Conversation) -> None:
        self._conn.execute(
            "INSERT INTO conversations "
            "(id, title_enc, created_at, default_logical_model_id, permission_preset) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                conversation.id,
                self._key.encrypt(conversation.title),
                _iso(conversation.created_at),
                conversation.default_logical_model_id,
                conversation.permission_preset.value,
            ),
        )

    def update(self, conversation: Conversation) -> None:
        self._conn.execute(
            "UPDATE conversations SET title_enc = ?, default_logical_model_id = ?, "
            "permission_preset = ? WHERE id = ?",
            (
                self._key.encrypt(conversation.title),
                conversation.default_logical_model_id,
                conversation.permission_preset.value,
                conversation.id,
            ),
        )

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT id, title_enc, created_at, default_logical_model_id, permission_preset "
            "FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_all(self) -> list[Conversation]:
        rows = self._conn.execute(
            "SELECT id, title_enc, created_at, default_logical_model_id, permission_preset "
            "FROM conversations ORDER BY created_at, id"
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def delete(self, conversation_id: str) -> None:
        """删除会话。schema 的 ON DELETE CASCADE 负责清掉分支/消息/运行/快照/镜像。"""
        self._conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def _to_domain(self, row: tuple[Any, ...]) -> Conversation:
        return Conversation(
            id=row[0],
            title=self._key.decrypt(row[1]),
            created_at=_parse_iso(row[2]),
            default_logical_model_id=row[3],
            permission_preset=PermissionPreset(row[4]),
        )


class _Branches:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, branch: Branch) -> None:
        self._conn.execute(
            "INSERT INTO branches "
            "(id, conversation_id, created_at, parent_branch_id, forked_from_message_id, "
            "title_enc) VALUES (?, ?, ?, ?, ?, ?)",
            (
                branch.id,
                branch.conversation_id,
                _iso(branch.created_at),
                branch.parent_branch_id,
                branch.forked_from_message_id,
                self._key.encrypt(branch.title) if branch.title is not None else None,
            ),
        )

    def update(self, branch: Branch) -> None:
        self._conn.execute(
            "UPDATE branches SET title_enc = ? WHERE id = ?",
            (
                self._key.encrypt(branch.title) if branch.title is not None else None,
                branch.id,
            ),
        )

    def get(self, branch_id: str) -> Branch | None:
        row = self._conn.execute(
            "SELECT id, conversation_id, created_at, parent_branch_id, forked_from_message_id, "
            "title_enc "
            "FROM branches WHERE id = ?",
            (branch_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_for_conversation(self, conversation_id: str) -> list[Branch]:
        rows = self._conn.execute(
            "SELECT id, conversation_id, created_at, parent_branch_id, forked_from_message_id, "
            "title_enc "
            "FROM branches WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def delete(self, branch_id: str) -> None:
        self._conn.execute("DELETE FROM branches WHERE id = ?", (branch_id,))

    def _to_domain(self, row: tuple[Any, ...]) -> Branch:
        return Branch(
            id=row[0],
            conversation_id=row[1],
            created_at=_parse_iso(row[2]),
            parent_branch_id=row[3],
            forked_from_message_id=row[4],
            title=self._key.decrypt(row[5]) if row[5] is not None else None,
        )


class _Messages:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, message: Message) -> None:
        self._conn.execute(
            "INSERT INTO messages "
            "(id, conversation_id, branch_id, role, content_enc, thinking_enc, status, "
            " created_at, run_id, pi_entry_id, is_whitelisted, tool_steps_enc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.conversation_id,
                message.branch_id,
                message.role.value,
                self._key.encrypt(message.content),
                self._key.encrypt(message.thinking) if message.thinking else "",
                message.status.value,
                _iso(message.created_at),
                message.run_id,
                message.pi_entry_id,
                1 if message.is_whitelisted else 0,
                self._key.encrypt(_dump_tool_steps(message.tool_steps)),
            ),
        )

    def update(self, message: Message) -> None:
        self._conn.execute(
            "UPDATE messages SET content_enc = ?, thinking_enc = ?, status = ?, "
            "run_id = ?, pi_entry_id = ?, is_whitelisted = ?, tool_steps_enc = ? "
            "WHERE id = ?",
            (
                self._key.encrypt(message.content),
                self._key.encrypt(message.thinking) if message.thinking else "",
                message.status.value,
                message.run_id,
                message.pi_entry_id,
                1 if message.is_whitelisted else 0,
                self._key.encrypt(_dump_tool_steps(message.tool_steps)),
                message.id,
            ),
        )

    _COLUMNS = (
        "id, conversation_id, branch_id, role, content_enc, thinking_enc, "
        "status, created_at, run_id, pi_entry_id, is_whitelisted, tool_steps_enc"
    )

    def get(self, message_id: str) -> Message | None:
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_for_branch(self, branch_id: str) -> list[Message]:
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM messages WHERE branch_id = ? ORDER BY created_at, id",
            (branch_id,),
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def count_for_branch(self, branch_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE branch_id = ?",
            (branch_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _to_domain(self, row: tuple[Any, ...]) -> Message:
        return Message(
            id=row[0],
            conversation_id=row[1],
            branch_id=row[2],
            role=MessageRole(row[3]),
            content=self._key.decrypt(row[4]),
            thinking=self._key.decrypt(row[5]) if row[5] else "",
            status=MessageStatus(row[6]),
            created_at=_parse_iso(row[7]),
            run_id=row[8],
            pi_entry_id=row[9],
            is_whitelisted=bool(row[10]),
            tool_steps=_load_tool_steps(self._key.decrypt(row[11])),
        )


class _Runs:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, run: RunRecord) -> None:
        self._conn.execute(
            "INSERT INTO runs "
            "(id, conversation_id, branch_id, status, created_at, finished_at, "
            " user_message_id, assistant_message_id, stop_reason, error_enc, retry_of_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.conversation_id,
                run.branch_id,
                run.status.value,
                _iso(run.created_at),
                _iso(run.finished_at) if run.finished_at else None,
                run.user_message_id,
                run.assistant_message_id,
                run.stop_reason,
                self._key.encrypt(run.error) if run.error else None,
                run.retry_of_message_id,
            ),
        )

    def update(self, run: RunRecord) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, assistant_message_id = ?, "
            "stop_reason = ?, error_enc = ? WHERE id = ?",
            (
                run.status.value,
                _iso(run.finished_at) if run.finished_at else None,
                run.assistant_message_id,
                run.stop_reason,
                self._key.encrypt(run.error) if run.error else None,
                run.id,
            ),
        )

    def get(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT id, conversation_id, branch_id, status, created_at, finished_at, "
            "user_message_id, assistant_message_id, stop_reason, error_enc, retry_of_message_id "
            "FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_for_conversation(self, conversation_id: str) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT id, conversation_id, branch_id, status, created_at, finished_at, "
            "user_message_id, assistant_message_id, stop_reason, error_enc, retry_of_message_id "
            "FROM runs WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def list_log_summaries(
        self, conversation_id: str, *, limit: int, offset: int,
    ) -> list[RunLogSummary]:
        rows = self._conn.execute(
            "SELECT r.id, r.status, r.created_at, "
            "(SELECT COUNT(*) FROM transport_snapshots t WHERE t.run_id = r.id) "
            "FROM runs r WHERE r.conversation_id = ? "
            "ORDER BY r.created_at, r.id LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        ).fetchall()
        return [RunLogSummary(r[0], RunStatus(r[1]), _parse_iso(r[2]), r[3]) for r in rows]

    def list_running(self) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT id, conversation_id, branch_id, status, created_at, finished_at, "
            "user_message_id, assistant_message_id, stop_reason, error_enc, retry_of_message_id "
            "FROM runs WHERE status = ? ORDER BY created_at, id",
            (RunStatus.RUNNING.value,),
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def _to_domain(self, row: tuple[Any, ...]) -> RunRecord:
        return RunRecord(
            id=row[0],
            conversation_id=row[1],
            branch_id=row[2],
            status=RunStatus(row[3]),
            created_at=_parse_iso(row[4]),
            finished_at=_parse_iso(row[5]) if row[5] else None,
            user_message_id=row[6],
            assistant_message_id=row[7],
            stop_reason=row[8],
            error=self._key.decrypt(row[9]) if row[9] else None,
            retry_of_message_id=row[10],
        )


class _Snapshots:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add_intent(self, snapshot: RequestIntentSnapshot) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO request_intents "
            "(id, run_id, conversation_id, branch_id, logical_model_id, endpoint_id, "
            " routing_reason, created_at, prompt_layers, message_ids, attachment_ids, app_params) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.run_id,
                snapshot.conversation_id,
                snapshot.branch_id,
                snapshot.logical_model_id,
                snapshot.endpoint_id,
                snapshot.routing_reason,
                _iso(snapshot.created_at),
                json.dumps(snapshot.prompt_layers, ensure_ascii=False),
                json.dumps(list(snapshot.message_ids), ensure_ascii=False),
                json.dumps(list(snapshot.attachment_ids), ensure_ascii=False),
                self._key.encrypt(json.dumps(snapshot.app_params, ensure_ascii=False)),
            ),
        )

    def add_transport(self, snapshot: TransportSnapshot) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO transport_snapshots "
            "(id, run_id, created_at, sequence, attempt, provider, model_id, url, "
            " headers_enc, body_enc, param_diff, response_status, stop_reason, error_class, "
            " response_body_enc, stream_tape_enc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.run_id,
                _iso(snapshot.created_at),
                snapshot.sequence,
                snapshot.attempt,
                snapshot.provider,
                snapshot.model_id,
                snapshot.url,
                self._key.encrypt(json.dumps(snapshot.headers, ensure_ascii=False)),
                self._key.encrypt(json.dumps(snapshot.body, ensure_ascii=False)),
                json.dumps(snapshot.param_diff, ensure_ascii=False),
                snapshot.response_status,
                snapshot.stop_reason,
                snapshot.error_class,
                self._key.encrypt(json.dumps(snapshot.response_body, ensure_ascii=False)),
                self._key.encrypt(json.dumps(snapshot.stream_tape, ensure_ascii=False)),
            ),
        )

    _INTENT_COLUMNS = (
        "id, run_id, conversation_id, branch_id, logical_model_id, endpoint_id, "
        "routing_reason, created_at, prompt_layers, message_ids, attachment_ids, app_params"
    )

    def get_intent(self, run_id: str) -> RequestIntentSnapshot | None:
        row = self._conn.execute(
            f"SELECT {self._INTENT_COLUMNS} FROM request_intents WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._to_intent(row) if row else None

    def list_all_intents(self) -> list[RequestIntentSnapshot]:
        rows = self._conn.execute(
            f"SELECT {self._INTENT_COLUMNS} FROM request_intents ORDER BY created_at, id"
        ).fetchall()
        return [self._to_intent(r) for r in rows]

    def _to_intent(self, row: tuple[Any, ...]) -> RequestIntentSnapshot:
        return RequestIntentSnapshot(
            id=row[0],
            run_id=row[1],
            conversation_id=row[2],
            branch_id=row[3],
            logical_model_id=row[4],
            endpoint_id=row[5],
            routing_reason=row[6],
            created_at=_parse_iso(row[7]),
            prompt_layers=json.loads(row[8]),
            message_ids=tuple(json.loads(row[9])),
            attachment_ids=tuple(json.loads(row[10])),
            app_params=json.loads(self._key.decrypt(row[11])),
        )

    def list_transport(self, run_id: str) -> list[TransportSnapshot]:
        rows = self._conn.execute(
            "SELECT id, run_id, created_at, sequence, attempt, provider, model_id, url, "
            "headers_enc, body_enc, param_diff, response_status, stop_reason, error_class, "
            "response_body_enc, stream_tape_enc "
            "FROM transport_snapshots WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        out: list[TransportSnapshot] = []
        for row in rows:
            out.append(
                TransportSnapshot(
                    id=row[0],
                    run_id=row[1],
                    created_at=_parse_iso(row[2]),
                    sequence=row[3],
                    attempt=row[4],
                    provider=row[5],
                    model_id=row[6],
                    url=row[7],
                    headers=json.loads(self._key.decrypt(row[8])),
                    body=json.loads(self._key.decrypt(row[9])),
                    param_diff=json.loads(row[10]),
                    response_status=row[11],
                    stop_reason=row[12],
                    error_class=row[13],
                    response_body=json.loads(self._key.decrypt(row[14])),
                    stream_tape=json.loads(self._key.decrypt(row[15])),
                )
            )
        return out


class _RuntimeMirror:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add_many(self, mirrors: list[RuntimeEntryMirror]) -> None:
        for mirror in mirrors:
            self._conn.execute(
                "INSERT OR IGNORE INTO runtime_mirrors "
                "(id, conversation_id, entry_id, entry_type, captured_at, run_id, "
                " parent_entry_id, message_id, runtime_instance_id, payload_enc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mirror.id,
                    mirror.conversation_id,
                    mirror.entry_id,
                    mirror.entry_type,
                    _iso(mirror.captured_at),
                    mirror.run_id,
                    mirror.parent_entry_id,
                    mirror.message_id,
                    mirror.runtime_instance_id,
                    self._key.encrypt(json.dumps(mirror.payload, ensure_ascii=False)),
                ),
            )

    def _merged(self, where: str, params: tuple[Any, ...]) -> list[RuntimeEntryMirror]:
        rows = self._conn.execute(
            "SELECT id, conversation_id, entry_id, entry_type, captured_at, run_id, "
            "parent_entry_id, message_id, runtime_instance_id, payload_enc "
            f"FROM runtime_mirrors {where} ORDER BY captured_at, id",
            params,
        ).fetchall()
        return [
            RuntimeEntryMirror(
                id=r[0],
                conversation_id=r[1],
                entry_id=r[2],
                entry_type=r[3],
                captured_at=_parse_iso(r[4]),
                run_id=r[5],
                parent_entry_id=r[6],
                message_id=r[7],
                runtime_instance_id=r[8],
                payload=json.loads(self._key.decrypt(r[9])),
            )
            for r in rows
        ]

    def list_for_conversation(self, conversation_id: str) -> list[RuntimeEntryMirror]:
        # 空串视为"取全部"（恢复协调器的用法，见 Phase 1C）
        if not conversation_id:
            return self._merged("", ())
        return self._merged("WHERE conversation_id = ?", (conversation_id,))

    def list_for_run(self, run_id: str) -> list[RuntimeEntryMirror]:
        return self._merged("WHERE run_id = ?", (run_id,))

    def link_message(self, mirror_id: str, message_id: str) -> None:
        cursor = self._conn.execute(
            "UPDATE runtime_mirrors SET message_id = ? WHERE id = ? AND message_id IS NULL",
            (message_id, mirror_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"运行时镜像不存在或已有关联，无法补链：{mirror_id}")


class _FileDocuments:
    """文件文档登记卡。display_name 加密（用户可命名），元数据明文。"""

    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, document: FileDocument) -> None:
        self._conn.execute(
            "INSERT INTO file_documents (id, kind, display_name_enc, path, blob_id, "
            "encoding, line_count, size_bytes, content_hash, mtime_ns, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document.id,
                document.kind.value,
                self._key.encrypt(document.display_name),
                document.path,
                document.blob_id,
                document.encoding,
                document.line_count,
                document.size_bytes,
                document.content_hash,
                document.mtime_ns,
                _iso(document.created_at),
            ),
        )

    def update(self, document: FileDocument) -> None:
        self._conn.execute(
            "UPDATE file_documents SET kind = ?, display_name_enc = ?, path = ?, blob_id = ?, "
            "encoding = ?, line_count = ?, size_bytes = ?, content_hash = ?, mtime_ns = ? "
            "WHERE id = ?",
            (
                document.kind.value,
                self._key.encrypt(document.display_name),
                document.path,
                document.blob_id,
                document.encoding,
                document.line_count,
                document.size_bytes,
                document.content_hash,
                document.mtime_ns,
                document.id,
            ),
        )

    def get(self, document_id: str) -> FileDocument | None:
        row = self._conn.execute(
            "SELECT id, kind, display_name_enc, path, blob_id, encoding, line_count, "
            "size_bytes, content_hash, mtime_ns, created_at FROM file_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_all(self) -> list[FileDocument]:
        rows = self._conn.execute(
            "SELECT id, kind, display_name_enc, path, blob_id, encoding, line_count, "
            "size_bytes, content_hash, mtime_ns, created_at FROM file_documents "
            "ORDER BY created_at, id"
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def delete(self, document_id: str) -> None:
        self._conn.execute("DELETE FROM file_documents WHERE id = ?", (document_id,))

    def _to_domain(self, row: tuple[Any, ...]) -> FileDocument:
        return FileDocument(
            id=row[0],
            kind=FileKind(row[1]),
            display_name=self._key.decrypt(row[2]),
            path=row[3],
            blob_id=row[4],
            encoding=row[5],
            line_count=row[6],
            size_bytes=row[7],
            content_hash=row[8],
            mtime_ns=row[9],
            created_at=_parse_iso(row[10]),
        )


class _ImageAttachments:
    """图片附件登记卡。原图字节在加密 blob 仓，这里只有元数据（全明文）。"""

    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn

    def add(self, attachment: ImageAttachment) -> None:
        self._conn.execute(
            "INSERT INTO image_attachments (id, format, blob_id, width, height, "
            "size_bytes, content_hash, source_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment.id,
                attachment.format.value,
                attachment.blob_id,
                attachment.width,
                attachment.height,
                attachment.size_bytes,
                attachment.content_hash,
                attachment.source_path,
                _iso(attachment.created_at),
            ),
        )

    def get(self, attachment_id: str) -> ImageAttachment | None:
        row = self._conn.execute(
            "SELECT id, format, blob_id, width, height, size_bytes, content_hash, "
            "source_path, created_at FROM image_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_all(self) -> list[ImageAttachment]:
        rows = self._conn.execute(
            "SELECT id, format, blob_id, width, height, size_bytes, content_hash, "
            "source_path, created_at FROM image_attachments ORDER BY created_at, id"
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def delete(self, attachment_id: str) -> None:
        self._conn.execute("DELETE FROM image_attachments WHERE id = ?", (attachment_id,))

    @staticmethod
    def _to_domain(row: tuple[Any, ...]) -> ImageAttachment:
        return ImageAttachment(
            id=row[0],
            format=ImageFormat(row[1]),
            blob_id=row[2],
            width=row[3],
            height=row[4],
            size_bytes=row[5],
            content_hash=row[6],
            source_path=row[7],
            created_at=_parse_iso(row[8]),
        )


class _Compressions:
    """压缩版本。摘要与错误文本加密（可能含会话内容）；启用标记是明文布尔。

    同一分支至多一个启用版本由 schema 的部分唯一索引兜底
    （``idx_compression_one_active``）；``set_active`` 在同一事务里先清后设。
    """

    _COLUMNS = (
        "id, conversation_id, branch_id, created_at, status, input_message_ids, "
        "tokens_before, tokens_after, compression_model_id, compression_endpoint_id, "
        "prompt_version, generated_summary_enc, edited_summary_enc, "
        "whitelist_message_ids, error_enc, is_active"
    )

    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, version: CompressionVersion) -> None:
        self._conn.execute(
            f"INSERT INTO compression_versions ({self._COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._to_row(version),
        )

    def update(self, version: CompressionVersion) -> None:
        # _to_row 的第 4 个字段起才是可更新列（id/conversation_id/branch_id/created_at 不动）
        self._conn.execute(
            "UPDATE compression_versions SET status = ?, input_message_ids = ?, "
            "tokens_before = ?, tokens_after = ?, compression_model_id = ?, "
            "compression_endpoint_id = ?, prompt_version = ?, generated_summary_enc = ?, "
            "edited_summary_enc = ?, whitelist_message_ids = ?, error_enc = ? "
            "WHERE id = ?",
            (*self._to_row(version)[4:15], version.id),
        )

    def get(self, version_id: str) -> CompressionVersion | None:
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM compression_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def list_for_branch(self, branch_id: str) -> list[CompressionVersion]:
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM compression_versions "
            "WHERE branch_id = ? ORDER BY created_at, id",
            (branch_id,),
        ).fetchall()
        return [self._to_domain(r) for r in rows]

    def get_active(self, branch_id: str) -> CompressionVersion | None:
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM compression_versions "
            "WHERE branch_id = ? AND is_active = 1",
            (branch_id,),
        ).fetchone()
        return self._to_domain(row) if row else None

    def set_active(self, branch_id: str, version_id: str) -> None:
        # 同一事务内先清后设（部分唯一索引保证至多一个）
        self._conn.execute(
            "UPDATE compression_versions SET is_active = 0 WHERE branch_id = ?",
            (branch_id,),
        )
        self._conn.execute(
            "UPDATE compression_versions SET is_active = 1 WHERE id = ? AND branch_id = ?",
            (version_id, branch_id),
        )

    def clear_active(self, branch_id: str) -> None:
        self._conn.execute(
            "UPDATE compression_versions SET is_active = 0 WHERE branch_id = ?",
            (branch_id,),
        )

    # ---------- 行映射 ----------

    def _to_row(self, v: CompressionVersion) -> tuple[Any, ...]:
        # is_active 恒为 0：启用标记只由 set_active/clear_active 管理，
        # 版本对象不携带启用状态（避免版本对象间状态不一致，见领域注释）
        return (
            v.id,
            v.conversation_id,
            v.branch_id,
            _iso(v.created_at),
            v.status.value,
            json.dumps(list(v.input_message_ids)),
            v.tokens_before,
            v.tokens_after,
            v.compression_model_id,
            v.compression_endpoint_id,
            v.prompt_version,
            self._key.encrypt(v.generated_summary),
            self._key.encrypt(v.edited_summary) if v.edited_summary is not None else None,
            json.dumps(list(v.whitelist_message_ids)),
            self._key.encrypt(v.error) if v.error is not None else None,
            0,
        )

    def _to_domain(self, row: tuple[Any, ...]) -> CompressionVersion:
        return CompressionVersion(
            id=row[0],
            conversation_id=row[1],
            branch_id=row[2],
            created_at=_parse_iso(row[3]),
            status=CompressionStatus(row[4]),
            input_message_ids=tuple(json.loads(row[5])),
            tokens_before=row[6],
            tokens_after=row[7],
            compression_model_id=row[8],
            compression_endpoint_id=row[9],
            prompt_version=row[10],
            generated_summary=self._key.decrypt(row[11]),
            edited_summary=self._key.decrypt(row[12]) if row[12] else None,
            whitelist_message_ids=tuple(json.loads(row[13])),
            error=self._key.decrypt(row[14]) if row[14] else None,
        )


class _Permissions:
    """权限授权（边界加密）+ 权限审计（参数与路径加密）。"""

    _GRANT_COLUMNS = (
        "id, conversation_id, capability, created_at, allowed_paths_enc, "
        "allowed_domains_enc, allowed_cwd_enc, command_classes_enc, note_enc"
    )
    _AUDIT_COLUMNS = (
        "id, conversation_id, created_at, tool_name, capability, params_enc, "
        "matched_rule, decision, risk, user_confirmed, actual_paths_enc"
    )

    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    # ----- 授权 -----

    def add_grant(self, grant: PermissionGrant) -> None:
        self._conn.execute(
            f"INSERT INTO permission_grants ({self._GRANT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant.id,
                grant.conversation_id,
                grant.capability.value,
                _iso(grant.created_at),
                self._key.encrypt(json.dumps(list(grant.allowed_paths))),
                self._key.encrypt(json.dumps(list(grant.allowed_domains))),
                self._key.encrypt(json.dumps(list(grant.allowed_working_directories))),
                self._key.encrypt(json.dumps(list(grant.command_classes))),
                self._key.encrypt(grant.note),
            ),
        )

    def list_grants(self, conversation_id: str) -> list[PermissionGrant]:
        rows = self._conn.execute(
            f"SELECT {self._GRANT_COLUMNS} FROM permission_grants "
            "WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [self._to_grant(r) for r in rows]

    def get_grant(self, grant_id: str) -> PermissionGrant | None:
        row = self._conn.execute(
            f"SELECT {self._GRANT_COLUMNS} FROM permission_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
        return self._to_grant(row) if row else None

    def delete_grant(self, grant_id: str) -> None:
        self._conn.execute("DELETE FROM permission_grants WHERE id = ?", (grant_id,))

    def _to_grant(self, row: tuple[Any, ...]) -> PermissionGrant:
        return PermissionGrant(
            id=row[0],
            conversation_id=row[1],
            capability=Capability(row[2]),
            created_at=_parse_iso(row[3]),
            allowed_paths=tuple(json.loads(self._key.decrypt(row[4]))),
            allowed_domains=tuple(json.loads(self._key.decrypt(row[5]))),
            allowed_working_directories=tuple(json.loads(self._key.decrypt(row[6]))),
            command_classes=tuple(json.loads(self._key.decrypt(row[7]))),
            note=self._key.decrypt(row[8]),
        )

    # ----- 审计 -----

    def add_audit(self, record: PermissionAudit) -> None:
        self._conn.execute(
            f"INSERT INTO permission_audit ({self._AUDIT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.conversation_id,
                _iso(record.created_at),
                record.tool_name,
                record.capability.value,
                self._key.encrypt(json.dumps(record.params, ensure_ascii=False)),
                record.matched_rule,
                record.decision.value,
                record.risk.value,
                1 if record.user_confirmed else 0,
                self._key.encrypt(json.dumps(list(record.actual_paths))),
            ),
        )

    def list_audit(self, conversation_id: str) -> list[PermissionAudit]:
        rows = self._conn.execute(
            f"SELECT {self._AUDIT_COLUMNS} FROM permission_audit "
            "WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [
            PermissionAudit(
                id=r[0],
                conversation_id=r[1],
                created_at=_parse_iso(r[2]),
                tool_name=r[3],
                capability=Capability(r[4]),
                params=json.loads(self._key.decrypt(r[5])),
                matched_rule=r[6],
                decision=Decision(r[7]),
                risk=RiskLevel(r[8]),
                user_confirmed=bool(r[9]),
                actual_paths=tuple(json.loads(self._key.decrypt(r[10]))),
            )
            for r in rows
        ]


def open_connection(db_path: Path) -> sqlite3.Connection:
    """打开一个已启用外键与 WAL 的连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level="DEFERRED")
    # SQLite 默认**不**执行 REFERENCES 约束，必须显式开启
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class SqliteUnitOfWorkFactory:
    """可关闭的连接工厂；重置前释放存量连接，并禁止迟到回调重新建库。"""

    def __init__(self, db_path: Path, key: VaultKey) -> None:
        self._path = db_path
        self._key = key
        self._closed = False
        self._units: set[SqliteUnitOfWork] = set()
        # sqlite Connection 的 with 只提交/回滚，并不会关闭连接。
        with closing(open_connection(db_path)) as conn, conn:
            migrate(conn)

    def __call__(self) -> SqliteUnitOfWork:
        if self._closed:
            raise RuntimeError("数据库已关闭，不能继续读写")
        unit = SqliteUnitOfWork(
            open_connection(self._path), self._key,
            close_on_exit=True, on_close=self._units.discard,
        )
        self._units.add(unit)
        return unit

    def close(self) -> None:
        self._closed = True
        for unit in list(self._units):
            unit.close()
        self._units.clear()


def sqlite_uow_factory(db_path: Path, key: VaultKey) -> SqliteUnitOfWorkFactory:
    """先迁移，再按事务打开独立连接；关闭工厂后不能重新开库。"""
    return SqliteUnitOfWorkFactory(db_path, key)
