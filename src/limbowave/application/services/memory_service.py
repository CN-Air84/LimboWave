"""全局/分支记忆、分叉快照和一次性模型写入授权。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.domain.memory import (
    MemoryDocument,
    MemoryItem,
    MemoryPolicy,
    MemoryRunContext,
    MemorySettings,
)
from limbowave.domain.permissions import Capability, Decision, PermissionAudit, RiskLevel

MAX_MEMORY_CHARS = 4000
MAX_SCOPE_CHARS = 32000
MAX_MEMORIES = 100


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


def _put_items(
    uow: UnitOfWork,
    items: list[MemoryItem],
    conversation_id: str | None,
    branch_id: str | None,
    *,
    document_id: str | None = None,
) -> None:
    if len(items) > MAX_MEMORIES or sum(len(i.content) for i in items) > MAX_SCOPE_CHARS:
        raise ValueError("记忆空间已满（最多100条、32000字），请先整理记忆")
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
    _put_items(uow, _items(uow.memories.get(f"snapshot:{message_id}")), conversation_id, branch_id)


class MemoryService:
    def __init__(self, uow_factory: UnitOfWorkFactory, configuration: ConfigurationService) -> None:
        self._uow_factory = uow_factory
        self._configuration = configuration
        self._approvals: dict[tuple[str, str], tuple[MemoryRunContext, str]] = {}
        self._approval_lock = Lock()

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
        with self._uow_factory() as uow:
            _validate_scope(uow, conversation_id, branch_id)
            return _items(uow.memories.get(_key(branch_id)))

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
        with self._uow_factory() as uow:
            _validate_scope(uow, conversation_id, branch_id)
            items = _items(uow.memories.get(_key(branch_id)))
            if item_id is not None and not any(i.id == item_id for i in items):
                raise ValueError("记忆不存在或不属于此分支")
            item = MemoryItem(
                item_id or uuid4().hex, content, "user", datetime.now(UTC).isoformat()
            )
            items = [item if i.id == item.id else i for i in items] if item_id else [*items, item]
            _put_items(uow, items, conversation_id, branch_id)
            uow.commit()
            return item

    def delete(
        self, item_id: str, conversation_id: str | None = None, branch_id: str | None = None
    ) -> None:
        with self._uow_factory() as uow:
            _validate_scope(uow, conversation_id, branch_id)
            items = _items(uow.memories.get(_key(branch_id)))
            if not any(i.id == item_id for i in items):
                raise ValueError("记忆不存在或不属于此分支")
            _put_items(uow, [i for i in items if i.id != item_id], conversation_id, branch_id)
            uow.commit()

    def promote(self, item_id: str, conversation_id: str, branch_id: str) -> MemoryItem:
        with self._uow_factory() as uow:
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
        with self._uow_factory() as uow:
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
        with self._uow_factory() as uow:
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
        with self._uow_factory() as uow:
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

    def clear_approvals(self) -> None:
        with self._approval_lock:
            self._approvals.clear()

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
        with self._uow_factory() as uow:
            _validate_scope(uow, context.conversation_id, context.branch_id)
            receipt = uow.memories.get(f"receipt:{source}")
            if receipt:
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

    def prompt_context(self, context: MemoryRunContext, round_number: int) -> dict[str, object]:
        settings = self.settings()
        return {
            "run_id": context.run_id,
            "branch_id": context.branch_id,
            "round": round_number,
            "global": [i.content for i in self.list()],
            "session": [i.content for i in self.list(context.conversation_id, context.branch_id)],
            "global_interval": settings.global_interval,
            "session_interval": settings.session_interval,
        }
