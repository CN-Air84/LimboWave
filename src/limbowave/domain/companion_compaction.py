"""Bounded chronological batches; source text is never silently dropped."""

from __future__ import annotations

from dataclasses import dataclass

from limbowave.domain.conversation import Message, MessageRole

COMPANION_BATCH_CHARS = 16000
COMPANION_SUMMARY_CHARS = 12000


@dataclass(frozen=True, slots=True)
class CompanionBatch:
    messages: tuple[str, ...]
    whitelist: tuple[str, ...]


def source_label(message: Message) -> str:
    role = {MessageRole.USER: "用户", MessageRole.ASSISTANT: "助手"}.get(
        message.role, message.role.value
    )
    return f"[来源 {message.id} | {message.created_at.isoformat()} | {role}]"


def companion_batches(
    messages: list[Message],
    *,
    max_chars: int = COMPANION_BATCH_CHARS,
) -> list[CompanionBatch]:
    """Split non-whitelisted long messages into lossless, source-labelled continuations.

    Whitelist messages remain atomic. A too-large whitelist fails explicitly so the
    user can reduce it; it is not silently shortened to meet a model's input budget.
    """
    batches: list[CompanionBatch] = []
    ordinary: list[str] = []
    whitelist: list[str] = []
    size = 0
    for message in messages:
        header = source_label(message) + "\n"
        capacity = max_chars - len(header)
        if capacity <= 0:
            raise ValueError("压缩分段预算不足以容纳消息来源")
        if message.is_whitelisted and len(message.content) > capacity:
            raise ValueError("白名单原文超过单段预算，请先调整白名单；未截断任何原文")
        parts = (
            [message.content]
            if message.is_whitelisted
            else [
                message.content[i : i + capacity] for i in range(0, len(message.content), capacity)
            ]
        )
        for part in parts or [""]:
            entry = header + part
            if size and size + len(entry) > max_chars:
                batches.append(CompanionBatch(tuple(ordinary), tuple(whitelist)))
                ordinary, whitelist, size = [], [], 0
            (whitelist if message.is_whitelisted else ordinary).append(entry)
            size += len(entry)
    if size or not batches:
        batches.append(CompanionBatch(tuple(ordinary), tuple(whitelist)))
    return batches
