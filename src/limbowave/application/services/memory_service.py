"""全局/分支记忆、分叉快照和一次性模型写入授权。"""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from threading import Lock, RLock
from uuid import uuid4

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.privacy_service import (
    PrivacyService,
    allowed_memory,
    load_policy,
)
from limbowave.domain.footprint import MemoryFootprint
from limbowave.domain.memory import (
    MemoryDocument,
    MemoryItem,
    MemoryMutation,
    MemoryPolicy,
    MemoryRunContext,
    MemorySettings,
    Scratchpad,
    SessionGlobalMemory,
)
from limbowave.domain.memory_presets import (
    BUILTIN_READER_PRESET_CONTENT,
    BUILTIN_READER_PRESET_ID,
)
from limbowave.domain.permissions import Capability, Decision, PermissionAudit, RiskLevel

MAX_MEMORY_CHARS = 4000
MAX_SCOPE_CHARS = 32000
MAX_MEMORIES = 100
MEMORY_MUTATION_TOOLS = frozenset({"update_memory", "delete_session_memory"})
MEMORY_CONTEXT_TOOLS = frozenset({
    "add_session_memory", "search_memory", "read_scratchpad", "write_scratchpad",
    *MEMORY_MUTATION_TOOLS,
})
# Shared across service instances: UI and model writes must not race (also in-memory UoW).
_MEMORY_WRITE_LOCK = RLock()


def _scratchpad(document: MemoryDocument | None) -> Scratchpad:
    return Scratchpad(**json.loads(document.payload)) if document else Scratchpad()


def _validate_receipt_scope(document: MemoryDocument, context: MemoryRunContext) -> None:
    if (document.conversation_id != context.conversation_id
            or document.branch_id != context.branch_id):
        raise ValueError("记忆回执不属于当前运行分支")


def _key(branch_id: str | None) -> str:
    return f"branch:{branch_id}" if branch_id else "global"


def _items(document: MemoryDocument | None) -> list[MemoryItem]:
    return [MemoryItem(**item) for item in json.loads(document.payload)] if document else []


def _validate_scope(uow: UnitOfWork, conversation_id: str | None, branch_id: str | None) -> None:
    if branch_id is None:
        if conversation_id is not None:
            raise ValueError("会话记忆必须指定分支")
        return
    branch = uow.branches.get(branch_id)
    if branch is None or branch.conversation_id != conversation_id:
        raise ValueError("会话或分支已失效")


def _validate_items(items: list[MemoryItem]) -> None:
    if len(items) > MAX_MEMORIES or sum(len(i.content) for i in items) > MAX_SCOPE_CHARS:
        raise ValueError("记忆空间已满（最多100条、32000字），请先整理记忆")


def _global_overrides(uow: UnitOfWork, conversation_id: str) -> dict[str, str | None]:
    if uow.conversations.get(conversation_id) is None:
        raise ValueError("会话不存在")
    document = uow.memories.get(f"global-overrides:{conversation_id}")
    overrides: dict[str, str | None] = json.loads(document.payload) if document else {}
    return overrides


def _session_global_items(
    items: list[MemoryItem], overrides: dict[str, str | None],
) -> list[SessionGlobalMemory]:
    result = []
    for item in items:
        if item.id not in overrides:
            result.append(SessionGlobalMemory(item, "inherited"))
        elif (content := overrides[item.id]) is None:
            result.append(SessionGlobalMemory(item, "deleted"))
        else:
            result.append(SessionGlobalMemory(replace(item, content=content), "modified"))
    return result


def _effective_global(uow: UnitOfWork, conversation_id: str) -> list[MemoryItem]:
    entries = _session_global_items(
        _items(uow.memories.get("global")), _global_overrides(uow, conversation_id),
    )
    return [entry.memory for entry in entries if entry.state != "deleted"]


def _put_items(
    uow: UnitOfWork,
    items: list[MemoryItem],
    conversation_id: str | None,
    branch_id: str | None,
    *,
    document_id: str | None = None,
) -> None:
    _validate_items(items)
    uow.memories.put(
        MemoryDocument(
            document_id or _key(branch_id),
            json.dumps([asdict(i) for i in items], ensure_ascii=False),
            conversation_id,
            branch_id,
        )
    )


def capture_memory(uow: UnitOfWork, message_id: str, conversation_id: str, branch_id: str) -> None:
    """与用户消息同事务保存；旧消息没有快照时分叉保守继承空列表。"""
    for prefix, empty in (("scratchpad", asdict(Scratchpad())), ("archive", [])):
        snapshot_key = f"{prefix}-snapshot:{message_id}"
        if uow.memories.get(snapshot_key) is None:
            document = uow.memories.get(f"{prefix}:{branch_id}")
            uow.memories.put(MemoryDocument(
                snapshot_key, document.payload if document else json.dumps(empty),
                conversation_id, branch_id,
            ))
    key = f"snapshot:{message_id}"
    if uow.memories.get(key) is None:
        _put_items(
            uow,
            _items(uow.memories.get(_key(branch_id))),
            conversation_id,
            branch_id,
            document_id=key,
        )


def fork_memory(uow: UnitOfWork, message_id: str, conversation_id: str, branch_id: str) -> None:
    for prefix, empty in (("scratchpad", asdict(Scratchpad())), ("archive", [])):
        document = uow.memories.get(f"{prefix}-snapshot:{message_id}")
        uow.memories.put(MemoryDocument(
            f"{prefix}:{branch_id}", document.payload if document else json.dumps(empty),
            conversation_id, branch_id,
        ))
    _put_items(uow, _items(uow.memories.get(f"snapshot:{message_id}")), conversation_id, branch_id)


class MemoryService:
    def __init__(self, uow_factory: UnitOfWorkFactory, configuration: ConfigurationService) -> None:
        self._uow_factory = uow_factory
        self._configuration = configuration
        self.privacy = PrivacyService(uow_factory)
        self._approvals: dict[tuple[str, str], tuple[MemoryRunContext, str]] = {}
        self._approval_lock = Lock()
        self._mutations: dict[tuple[str, str], tuple[MemoryRunContext, MemoryMutation]] = {}

    def ensure_builtin_preset(self) -> bool:
        """启动时一次性写入普通全局记忆；空间不足时延后，不覆盖修改或删除。"""
        receipt_id = f"preset-installed:{BUILTIN_READER_PRESET_ID}"
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            if uow.memories.get(receipt_id) is not None:
                return False
            items = _items(uow.memories.get("global"))
            installed = any(item.id == BUILTIN_READER_PRESET_ID for item in items)
            if not installed:
                content = self.validate_content(BUILTIN_READER_PRESET_CONTENT)
                if (
                    len(items) >= MAX_MEMORIES
                    or sum(len(item.content) for item in items) + len(content) > MAX_SCOPE_CHARS
                ):
                    return False
                item = MemoryItem(
                    BUILTIN_READER_PRESET_ID, content, "builtin", datetime.now(UTC).isoformat(),
                )
                _put_items(uow, [*items, item], None, None)
            # 独立回执与正文同事务保存：用户清空全局列表后，也不能自动补回预设。
            uow.memories.put(MemoryDocument(
                receipt_id, json.dumps({"item_id": BUILTIN_READER_PRESET_ID}),
            ))
            uow.commit()
            return not installed

    def timeline(self) -> builtins.list[MemoryFootprint]:
        """Read a local projection; never invoke the model or materialize another archive."""
        from limbowave.application.services.memory_timeline import build_memory_timeline

        with self._uow_factory() as uow:
            uow.begin_read()
            return build_memory_timeline(uow)

    def settings(self) -> MemorySettings:
        return self._configuration.load().memory

    def save_settings(self, settings: MemorySettings) -> None:
        config = self._configuration.load()
        self._configuration.save(
            type(config).model_validate({**config.model_dump(), "memory": settings.model_dump()})
        )

    def list(
        self, conversation_id: str | None = None, branch_id: str | None = None
    ) -> list[MemoryItem]:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            return _items(uow.memories.get(_key(branch_id)))

    def list_session_global(self, conversation_id: str) -> builtins.list[SessionGlobalMemory]:
        """列出全局记忆在此会话的状态，包含可恢复的删除项。"""
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            return _session_global_items(
                _items(uow.memories.get("global")), _global_overrides(uow, conversation_id),
            )

    def effective_global(self, conversation_id: str) -> builtins.list[MemoryItem]:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            return _effective_global(uow, conversation_id)

    def save_session_global(self, item_id: str, content: str, conversation_id: str) -> None:
        self._set_session_global(item_id, self.validate_content(content), conversation_id)

    def delete_session_global(self, item_id: str, conversation_id: str) -> None:
        self._set_session_global(item_id, None, conversation_id)

    def restore_session_global(self, item_id: str, conversation_id: str) -> None:
        self._set_session_global(item_id, None, conversation_id, restore=True)

    def _set_session_global(
        self, item_id: str, content: str | None, conversation_id: str, *, restore: bool = False,
    ) -> None:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            overrides = _global_overrides(uow, conversation_id)
            originals = _items(uow.memories.get("global"))
            live_ids = {item.id for item in originals}
            if item_id not in live_ids:
                raise ValueError("全局记忆不存在，请刷新后重试")
            # 不保留全局原文已删除的孤立覆盖；它们不能成为新的会话记忆。
            overrides = {key: value for key, value in overrides.items() if key in live_ids}
            if restore:
                overrides.pop(item_id, None)
            else:
                overrides[item_id] = content
            if content is not None:
                _validate_items([
                    entry.memory for entry in _session_global_items(originals, overrides)
                    if entry.state != "deleted"
                ])
            uow.memories.put(MemoryDocument(
                f"global-overrides:{conversation_id}",
                json.dumps(overrides, ensure_ascii=False),
                conversation_id,
            ))
            uow.commit()

    @staticmethod
    def validate_content(content: str) -> str:
        content = content.strip()
        if not content or len(content) > MAX_MEMORY_CHARS:
            raise ValueError("记忆正文需为1–4000字")
        return content

    def save(
        self,
        content: str,
        conversation_id: str | None = None,
        branch_id: str | None = None,
        *,
        item_id: str | None = None,
    ) -> MemoryItem:
        content = self.validate_content(content)
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            items = _items(uow.memories.get(_key(branch_id)))
            original = next((i for i in items if i.id == item_id), None)
            if item_id is not None and original is None:
                raise ValueError("记忆不存在或不属于此分支")
            if original is not None and original.content == content:
                return original
            now = datetime.now(UTC).isoformat()
            item = (
                replace(original, content=content, updated_at=now)
                if original is not None else MemoryItem(uuid4().hex, content, "user", now)
            )
            items = [item if i.id == item.id else i for i in items] if item_id else [*items, item]
            _put_items(uow, items, conversation_id, branch_id)
            uow.commit()
            return item

    def delete(
        self, item_id: str, conversation_id: str | None = None, branch_id: str | None = None
    ) -> None:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            items = _items(uow.memories.get(_key(branch_id)))
            if not any(i.id == item_id for i in items):
                raise ValueError("记忆不存在或不属于此分支")
            _put_items(uow, [i for i in items if i.id != item_id], conversation_id, branch_id)
            uow.commit()

    def promote(self, item_id: str, conversation_id: str, branch_id: str) -> MemoryItem:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            original = next(
                (i for i in _items(uow.memories.get(_key(branch_id))) if i.id == item_id), None
            )
            if original is None:
                raise ValueError("记忆不存在或不属于此分支")
            items = _items(uow.memories.get("global"))
            source = f"promoted:{branch_id}:{item_id}"
            existing = next(
                (i for i in items if i.source == source and i.content == original.content), None
            )
            if existing:
                return existing
            item = MemoryItem(uuid4().hex, original.content, source, datetime.now(UTC).isoformat())
            _put_items(uow, [*items, item], None, None)
            uow.commit()
            return item

    def policy(self, conversation_id: str) -> MemoryPolicy:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            doc = uow.memories.get(f"policy:{conversation_id}")
            return MemoryPolicy(json.loads(doc.payload)) if doc else MemoryPolicy.INHERIT

    def effective_policy(self, conversation_id: str) -> MemoryPolicy:
        policy = self.policy(conversation_id)
        return (
            MemoryPolicy(self.settings().default_policy)
            if policy is MemoryPolicy.INHERIT
            else policy
        )

    def set_policy(self, conversation_id: str, policy: MemoryPolicy) -> None:
        policy = MemoryPolicy(policy)
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            if uow.conversations.get(conversation_id) is None:
                raise ValueError("会话不存在")
            uow.memories.put(
                MemoryDocument(
                    f"policy:{conversation_id}", json.dumps(policy.value), conversation_id
                )
            )
            uow.commit()

    def record_decision(
        self,
        context: MemoryRunContext,
        call_id: str,
        content: str,
        *,
        allowed: bool,
        asked: bool,
    ) -> None:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            uow.permissions.add_audit(
                PermissionAudit(
                    id=uuid4().hex,
                    conversation_id=context.conversation_id,
                    created_at=datetime.now(UTC),
                    tool_name="add_session_memory",
                    capability=Capability.MEMORY_WRITE,
                    params={
                        "content": content,
                        "call_id": call_id,
                        "run_id": context.run_id,
                        "branch_id": context.branch_id,
                    },
                    matched_rule="memory.ask" if asked else "memory.allow",
                    decision=Decision.ALLOW if allowed else Decision.DENY,
                    risk=RiskLevel.NORMAL,
                    user_confirmed=allowed and asked,
                )
            )
            uow.commit()

    def approve(self, context: MemoryRunContext, call_id: str, content: str) -> None:
        with self._approval_lock:
            self._approvals[(context.run_id, call_id)] = (context, self.validate_content(content))

    def discard_approval(self, context: MemoryRunContext, call_id: str) -> None:
        """A new gate decision for the same call must supersede an unconsumed decision."""
        with self._approval_lock:
            key = context.run_id, call_id
            self._approvals.pop(key, None)
            self._mutations.pop(key, None)

    def clear_approvals(self) -> None:
        with self._approval_lock:
            self._approvals.clear()
            self._mutations.clear()

    def add_from_model(
        self,
        context: MemoryRunContext,
        call_id: str,
        content: str,
        is_active: Callable[[MemoryRunContext], bool],
    ) -> MemoryItem:
        content = self.validate_content(content)
        if not call_id or not is_active(context):
            raise ValueError("记忆工具所属运行已失效")
        source = f"model:{context.run_id}:{call_id}"
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, context.conversation_id, context.branch_id)
            receipt = uow.memories.get(f"receipt:{source}")
            if receipt:
                _validate_receipt_scope(receipt, context)
                item = MemoryItem(**json.loads(receipt.payload))
                if item.content != content:
                    raise ValueError("重复工具调用的正文不一致")
                return item
            with self._approval_lock:
                approval = self._approvals.pop((context.run_id, call_id), None)
            if approval != (context, content):
                raise ValueError("记忆写入缺少有效的一次性授权")
            items = _items(uow.memories.get(_key(context.branch_id)))
            item = MemoryItem(uuid4().hex, content, source, datetime.now(UTC).isoformat())
            _put_items(uow, [*items, item], context.conversation_id, context.branch_id)
            uow.memories.put(
                MemoryDocument(
                    f"receipt:{source}",
                    json.dumps(asdict(item), ensure_ascii=False),
                    context.conversation_id,
                    context.branch_id,
                )
            )
            if not is_active(context):
                raise ValueError("记忆工具所属运行已失效")
            uow.commit()
            return item

    def privacy_allows_model_memory(self, context: MemoryRunContext) -> bool:
        with self._uow_factory() as uow:
            policy = load_policy(uow, context.conversation_id)
            if not policy.rules:
                return True
            message = uow.messages.get(context.user_message_id)
            return (message is not None
                    and not policy.blocks(message.content, message.id))

    def prompt_context(self, context: MemoryRunContext, round_number: int) -> dict[str, object]:
        settings = self.settings()
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, context.conversation_id, context.branch_id)
            policy = load_policy(uow, context.conversation_id)
            global_items = [i for i in _effective_global(uow, context.conversation_id)
                            if allowed_memory(policy, i, global_item=True)]
            session_items = [i for i in _items(uow.memories.get(_key(context.branch_id)))
                             if allowed_memory(policy, i)]
            pad = _scratchpad(uow.memories.get(f"scratchpad:{context.branch_id}"))
        return {
            "run_id": context.run_id,
            "branch_id": context.branch_id,
            "round": round_number,
            "global": [i.content for i in global_items],
            "session": [i.content for i in session_items],
            "global_interval": settings.global_interval,
            "session_interval": settings.session_interval,
            "scratchpad": asdict(Scratchpad() if policy.rules else pad),
        }

    def search(
        self, conversation_id: str, branch_id: str, query: str,
        scope: str = "all", limit: int = 10,
    ) -> dict[str, object]:
        """Literal AND-token search; never scan foreign branches or hidden global entries."""
        if not query.strip() or len(query) > 500:
            raise ValueError("检索词需为1–500字")
        if scope not in {"all", "session", "global", "archived"}:
            raise ValueError("scope 必须为 all/session/global/archived")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit 必须为1–20的整数")
        tokens = query.strip().casefold().split()
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            policy = load_policy(uow, conversation_id)
            groups: dict[str, builtins.list[MemoryItem]] = {}
            if scope in {"all", "session"}:
                groups["session"] = _items(uow.memories.get(_key(branch_id)))
            if scope in {"all", "global"}:
                groups["global"] = _effective_global(uow, conversation_id)
            if scope == "archived":
                groups["archived"] = _items(uow.memories.get(f"archive:{branch_id}"))
        matches = []
        for kind, items in groups.items():
            for item in items:
                if not allowed_memory(policy, item, global_item=kind == "global"):
                    continue
                text = item.content.casefold()
                if query.strip() == item.id or all(token in text for token in tokens):
                    title = item.content.splitlines()[0].casefold()
                    score = (10000 if query.strip() == item.id else
                             sum(3 * (token in title) + text.count(token) for token in tokens))
                    matches.append((score, kind, item))
        matches.sort(key=lambda row: (-row[0], row[1], row[2].id))
        results = []
        for _score, kind, item in matches[:limit]:
            text = item.content
            position = text.casefold().find(tokens[0])
            start = 0
            if len(text) > 1000 and position > 0:
                # casefold can expand a character (e.g. ß → ss); use original offsets.
                folded_offset = 0
                for offset, character in enumerate(text):
                    if folded_offset >= position:
                        start = max(0, offset - 100)
                        break
                    folded_offset += len(character.casefold())
            excerpt = text if query.strip() == item.id else text[start:start + 1000]
            results.append({
                "id": item.id, "scope": kind, "content": excerpt,
                "model_writable": kind in {"session", "global"},
                "allowed_operations": (
                    ["update_memory", "delete_session_memory"] if kind == "session"
                    else ["update_memory"] if kind == "global" else []
                ),
                "content_truncated": excerpt != text,
                "created_at": item.created_at,
            })
        return {"results": results, "total": len(matches), "has_more": len(matches) > limit}

    def list_archived(self, conversation_id: str, branch_id: str) -> builtins.list[MemoryItem]:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            return _items(uow.memories.get(f"archive:{branch_id}"))

    def delete_archived(self, item_id: str, conversation_id: str, branch_id: str) -> None:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            key = f"archive:{branch_id}"
            items = _items(uow.memories.get(key))
            if not any(i.id == item_id for i in items):
                raise ValueError("归档记忆不存在")
            _put_items(uow, [i for i in items if i.id != item_id],
                       conversation_id, branch_id, document_id=key)
            uow.commit()

    def restore_archived(self, item_id: str, conversation_id: str, branch_id: str) -> None:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            archived = _items(uow.memories.get(f"archive:{branch_id}"))
            original = next((i for i in archived if i.id == item_id), None)
            if original is None:
                raise ValueError("归档记忆不存在")
            current = _items(uow.memories.get(_key(branch_id)))
            if any(i.id == item_id for i in current):
                raise ValueError("当前分支已存在同 ID 记忆")
            _put_items(uow, [*current, original], conversation_id, branch_id)
            _put_items(uow, [i for i in archived if i.id != item_id],
                       conversation_id, branch_id, document_id=f"archive:{branch_id}")
            uow.commit()

    def prepare_mutation(
        self, context: MemoryRunContext, tool_name: str, memory_id: str,
        updated_content: str | None = None, *, archive: bool = True,
    ) -> MemoryMutation:
        if tool_name not in MEMORY_MUTATION_TOOLS or type(archive) is not bool:
            raise ValueError("无效的记忆操作")
        if tool_name == "update_memory":
            if updated_content is None:
                raise ValueError("缺少更新正文")
            updated_content = self.validate_content(updated_content)
        elif updated_content is not None:
            raise ValueError("删除操作不接受正文")
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, context.conversation_id, context.branch_id)
            original = next((i for i in _items(uow.memories.get(_key(context.branch_id)))
                             if i.id == memory_id), None)
            if original is not None:
                return MemoryMutation(tool_name, original, updated_content, archive)
            if tool_name == "update_memory" and any(
                item.id == memory_id for item in _effective_global(uow, context.conversation_id)
            ):
                # Show/approve the actual global original, not a conversation's override.
                original = next(i for i in _items(uow.memories.get("global")) if i.id == memory_id)
                return MemoryMutation(tool_name, original, updated_content, archive, scope="global")
        raise ValueError("记忆不存在或当前会话不可见；归档/删除只支持当前分支会话记忆")

    def approve_mutation(
        self, context: MemoryRunContext, call_id: str, change: MemoryMutation,
    ) -> None:
        with self._approval_lock:
            self._mutations[(context.run_id, call_id)] = (context, change)

    def mutation_for_call(
        self, context: MemoryRunContext, call_id: str, tool_name: str, memory_id: str,
        updated_content: str | None = None, *, archive: bool = True,
    ) -> MemoryMutation:
        """Resolve the *approved* original, not a new snapshot taken after confirmation."""
        if updated_content is not None:
            updated_content = self.validate_content(updated_content)
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            receipt = uow.memories.get(f"receipt:model:{context.run_id}:{call_id}")
            if receipt:
                _validate_receipt_scope(receipt, context)
                data = json.loads(receipt.payload)["change"]
                change = MemoryMutation(**{**data, "original": MemoryItem(**data["original"])})
            else:
                with self._approval_lock:
                    approval = self._mutations.get((context.run_id, call_id))
                if approval is None or approval[0] != context:
                    raise ValueError("记忆变更缺少有效的一次性授权")
                change = approval[1]
        if (change.tool_name, change.original.id, change.updated_content, change.archive) != (
            tool_name, memory_id, updated_content, archive,
        ):
            raise ValueError("记忆操作与已审批内容不一致")
        return change

    def mutate_from_model(
        self, context: MemoryRunContext, call_id: str, change: MemoryMutation,
        is_active: Callable[[MemoryRunContext], bool],
    ) -> dict[str, object]:
        if not call_id or not is_active(context):
            raise ValueError("记忆工具所属运行已失效")
        if change.scope not in {"session", "global"} or (
            change.scope == "global" and change.tool_name != "update_memory"
        ):
            raise ValueError("此操作不支持指定的记忆范围")
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, context.conversation_id, context.branch_id)
            key = f"receipt:model:{context.run_id}:{call_id}"
            receipt = uow.memories.get(key)
            if receipt:
                _validate_receipt_scope(receipt, context)
                data = json.loads(receipt.payload)
                saved_change = data.get("change", {})
                if {"scope": "session", **saved_change} != asdict(change):
                    raise ValueError("重复工具调用的操作不一致")
                result: dict[str, object] = data["result"]
                return result
            with self._approval_lock:
                approval = self._mutations.pop((context.run_id, call_id), None)
            if approval != (context, change):
                raise ValueError("记忆变更缺少有效的一次性授权")
            global_scope = change.scope == "global"
            if global_scope and not any(
                i.id == change.original.id for i in _effective_global(uow, context.conversation_id)
            ):
                raise ValueError("全局记忆在审批后已不可见，请重新检索并征得同意")
            target_branch = None if global_scope else context.branch_id
            target_conversation = None if global_scope else context.conversation_id
            items = _items(uow.memories.get(_key(target_branch)))
            original = next((i for i in items if i.id == change.original.id), None)
            if original != change.original:
                raise ValueError("记忆在审批后发生变化，请重新检索并征得同意")
            if change.tool_name == "update_memory":
                content = self.validate_content(change.updated_content or "")
                replacement = replace(
                    original, content=content, updated_at=datetime.now(UTC).isoformat(),
                )
                items = [replacement if i.id == original.id else i for i in items]
                action = "updated"
            elif change.tool_name == "delete_session_memory":
                items = [i for i in items if i.id != original.id]
                action = "archived" if change.archive else "deleted"
                if change.archive:
                    archive_key = f"archive:{context.branch_id}"
                    archived = _items(uow.memories.get(archive_key))
                    _put_items(uow, [*archived, original], context.conversation_id,
                               context.branch_id, document_id=archive_key)
            else:
                raise ValueError("无效的记忆操作")
            _put_items(uow, items, target_conversation, target_branch)
            result = {"id": original.id, "action": action, "saved": True, "scope": change.scope}
            uow.memories.put(MemoryDocument(
                key, json.dumps({"change": asdict(change), "result": result}, ensure_ascii=False),
                context.conversation_id, context.branch_id,
            ))
            if not is_active(context):
                raise ValueError("记忆工具所属运行已失效")
            uow.commit()
            return result

    def record_mutation_decision(
        self, context: MemoryRunContext, call_id: str, change: MemoryMutation, *, allowed: bool,
    ) -> None:
        with self._uow_factory() as uow:
            uow.permissions.add_audit(PermissionAudit(
                id=uuid4().hex, conversation_id=context.conversation_id,
                created_at=datetime.now(UTC), tool_name=change.tool_name,
                capability=Capability.MEMORY_WRITE,
                params={"call_id": call_id, "run_id": context.run_id,
                        "branch_id": context.branch_id, **asdict(change)},
                matched_rule="memory.mutation.ask",
                decision=Decision.ALLOW if allowed else Decision.DENY,
                risk=RiskLevel.HIGH if change.scope == "global" else RiskLevel.NORMAL,
                user_confirmed=allowed,
            ))
            uow.commit()

    def read_scratchpad(self, conversation_id: str, branch_id: str) -> Scratchpad:
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            return _scratchpad(uow.memories.get(f"scratchpad:{branch_id}"))

    def write_scratchpad(
        self, conversation_id: str, branch_id: str, content: str, expected_revision: int,
        *, is_active: Callable[[], bool] = lambda: True,
    ) -> Scratchpad:
        if len(content) > MAX_MEMORY_CHARS:
            raise ValueError("便签最多4000字，请先整理")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须为非负整数")
        with _MEMORY_WRITE_LOCK, self._uow_factory() as uow:
            uow.begin_read()
            _validate_scope(uow, conversation_id, branch_id)
            key = f"scratchpad:{branch_id}"
            current = _scratchpad(uow.memories.get(key))
            if current.revision != expected_revision:
                raise ValueError("便签已发生变化，请重新读取后再编辑（未覆盖现有便签）")
            pad = Scratchpad(content.strip(), current.revision + 1)
            uow.memories.put(MemoryDocument(
                key, json.dumps(asdict(pad), ensure_ascii=False), conversation_id, branch_id,
            ))
            if not is_active():
                raise ValueError("便签工具所属运行已失效")
            uow.commit()
            return pad
