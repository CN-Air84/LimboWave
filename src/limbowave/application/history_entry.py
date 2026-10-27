"""Lightweight history display DTO shared by application and UI layers."""

from __future__ import annotations

from typing import NamedTuple

from limbowave.domain.compaction import CompressionContext
from limbowave.domain.conversation import AssistantMessageSegment
from limbowave.domain.tool_step import ToolStep


class HistoryEntry(NamedTuple):
    """One persisted history item ready for presentation."""

    role: str
    content: str
    thinking: str
    message_id: str | None
    run_id: str | None = None
    tool_steps: tuple[ToolStep, ...] = ()
    retry_available: bool = False
    can_fork: bool = True
    segments: tuple[AssistantMessageSegment, ...] = ()
    compressions: tuple[CompressionContext, ...] = ()
