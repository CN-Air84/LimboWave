"""Encrypted, durable privacy boundaries shared by every model input path."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.memory import MemoryDocument, MemoryItem
from limbowave.domain.privacy import PrivacyPolicy, TopicBoundary, content_fingerprint, normalized

PRIVACY_KEY = "privacy:global"
_LOCK = RLock()
_MODEL_READERS = 0


@contextmanager
def privacy_model_request() -> Iterator[None]:
    """Do not let an isolated title/summary request race a user rule change."""
    global _MODEL_READERS
    with _LOCK:
        _MODEL_READERS += 1
    try:
        yield
    finally:
        with _LOCK:
            _MODEL_READERS -= 1


def load_policy(uow: UnitOfWork, conversation_id: str | None = None) -> PrivacyPolicy:
    document = uow.memories.get(PRIVACY_KEY)
    # Deliberately propagate malformed/decryption failures; never silently fail open.
    policy = PrivacyPolicy.model_validate_json(document.payload) if document else PrivacyPolicy()
    if conversation_id:
        local = uow.memories.get(f"privacy:conversation:{conversation_id}")
        if local:
            fresh = PrivacyPolicy.model_validate_json(local.payload).fresh_start
            policy = policy.model_copy(update={"fresh_start": fresh})
    return policy


def allowed_messages(policy: PrivacyPolicy, messages: Sequence[Message]) -> list[Message]:
    allowed = []
    blocked_turn = False
    for message in messages:
        blocked = policy.blocks(message.content, message.id)
        if message.role is MessageRole.USER:
            blocked_turn = blocked
        if blocked or blocked_turn:
            continue
        if (message.role is MessageRole.ASSISTANT and policy.rules
                and (message.run_id is None or not policy.permits_derived(message.created_at))):
            continue
        allowed.append(message)
    return allowed


def allowed_memory(policy: PrivacyPolicy, item: MemoryItem, *, global_item: bool = False) -> bool:
    if (global_item and policy.fresh_start) or policy.blocks(item.content, item.id):
        return False
    if not policy.rules:
        return True
    try:
        created = datetime.fromisoformat(item.created_at)
        return created.tzinfo is not None and policy.permits_derived(created)
    except (ValueError, TypeError):
        return False


class PrivacyService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory
        self.is_busy: Callable[[], bool] = lambda: False

    def policy(self, conversation_id: str | None = None) -> PrivacyPolicy:
        with self._uow_factory() as uow:
            uow.begin_read()
            return load_policy(uow, conversation_id)

    def _check_idle(self) -> None:
        if self.is_busy() or _MODEL_READERS:
            raise ValueError(
                "请先停止当前回复并等待收尾，再修改翻篇与话题禁区；已发出的内容无法撤回。"
            )

    def add(self, topic: str, aliases: Sequence[str] = (),
            source_ids: Sequence[str] = ()) -> TopicBoundary:
        self._check_idle()
        topic = topic.strip()
        terms = tuple(dict.fromkeys(term.strip() for term in aliases if term.strip()))
        if not normalized(topic) or len(topic) > 200 or any(len(t) > 200 for t in terms):
            raise ValueError("话题及别名需为1–200字")
        rule = TopicBoundary(id=f"privacy_{uuid4().hex}", topic=topic, aliases=terms,
                             source_ids=tuple(source_ids), created_at=datetime.now(UTC))
        with _LOCK, self._uow_factory() as uow:
            self._check_idle()
            policy = load_policy(uow)
            if len(policy.rules) >= 100:
                raise ValueError("最多保存100条话题禁区")
            if any(normalized(r.topic) == normalized(topic) for r in policy.rules):
                raise ValueError("该话题已在禁区中")
            # Bind exact source copies too: edited/forked copies may acquire new IDs.
            hashes = set()
            for source_id in rule.source_ids:
                message = uow.messages.get(source_id)
                if message is not None:
                    hashes.add(content_fingerprint(message.content))
            if rule.source_ids:
                for document in uow.memories.list_current():
                    for item in json.loads(document.payload):
                        if item["id"] in rule.source_ids:
                            hashes.add(content_fingerprint(item["content"]))
            rule = TopicBoundary.model_validate({
                **rule.model_dump(), "content_hashes": tuple(sorted(hashes)),
            })
            updated = policy.model_copy(update={"rules": (*policy.rules, rule)})
            uow.memories.put(MemoryDocument(PRIVACY_KEY, updated.model_dump_json()))
            uow.commit()
        return rule

    def remove(self, rule_id: str) -> None:
        with _LOCK, self._uow_factory() as uow:
            self._check_idle()
            policy = load_policy(uow)
            updated = policy.model_copy(update={
                "rules": tuple(rule for rule in policy.rules if rule.id != rule_id),
            })
            uow.memories.put(MemoryDocument(PRIVACY_KEY, updated.model_dump_json()))
            uow.commit()

    def filter_messages(self, messages: Sequence[Message]) -> list[Message]:
        return allowed_messages(self.policy(), messages)

    @staticmethod
    def mark_fresh(uow: UnitOfWork, conversation_id: str) -> None:
        uow.memories.put(MemoryDocument(
            f"privacy:conversation:{conversation_id}",
            PrivacyPolicy(fresh_start=True).model_dump_json(), conversation_id,
        ))
