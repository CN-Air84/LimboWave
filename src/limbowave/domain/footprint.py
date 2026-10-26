"""Read-only memory footprints; timestamps are instants, never inferred history."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from limbowave.domain.memory import MemoryItem


def memory_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class FootprintLocation:
    conversation_id: str
    branch_id: str
    conversation_title: str
    branch_title: str


@dataclass(frozen=True, slots=True)
class MemoryFootprint:
    memory: MemoryItem
    scope: Literal["global", "session"]
    locations: tuple[FootprintLocation, ...] = ()
    origin: FootprintLocation | None = None
    source_message_id: str | None = None
    written_at: str | None = None

    @property
    def source_kind(self) -> str:
        return self.memory.source.split(":", 1)[0]
