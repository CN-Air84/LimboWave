"""One-shot reminders; only metadata is stored in plaintext by adapters."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReminderStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReminderContext:
    conversation_id: str
    branch_id: str


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    conversation_id: str
    branch_id: str
    target_time: datetime
    reason: str
    created_at: datetime
    status: ReminderStatus = ReminderStatus.PENDING
    completed_at: datetime | None = None
