"""Encrypted reminder reasons, indexed UTC deadlines and transactional state transitions."""
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from limbowave.domain.reminder import Reminder, ReminderRecurrence, ReminderStatus
from limbowave.infrastructure.crypto.vault import VaultKey

_COLUMNS = ("id, conversation_id, branch_id, target_time, reason, created_at, status, "
            "completed_at, details_enc")


class SqliteReminderRepository:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def add(self, reminder: Reminder) -> None:
        self._conn.execute(
            "INSERT INTO reminders (" + _COLUMNS
            + ", due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reminder.id, reminder.conversation_id, reminder.branch_id,
             reminder.target_time.isoformat(), self._key.encrypt(reminder.reason),
             reminder.created_at.isoformat(), reminder.status.value,
             reminder.completed_at.isoformat() if reminder.completed_at else None,
             self._key.encrypt(json.dumps({
                 "recurrence": asdict(reminder.recurrence) if reminder.recurrence else None,
                 "source_message_id": reminder.source_message_id,
             })),
             reminder.target_time.astimezone(UTC).isoformat()),
        )

    def get(self, reminder_id: str) -> Reminder | None:
        row = self._conn.execute(
            "SELECT " + _COLUMNS + " FROM reminders WHERE id=?", (reminder_id,),
        ).fetchone()
        return self._decode(row) if row else None

    def list_for_conversation(
        self, conversation_id: str, *, status: str = "pending", limit: int = 50, offset: int = 0,
    ) -> list[Reminder]:
        where = "conversation_id=?" + (" AND status=?" if status != "all" else "")
        params = (conversation_id, status) if status != "all" else (conversation_id,)
        rows = self._conn.execute(
            "SELECT " + _COLUMNS + " FROM reminders WHERE " + where
            + " ORDER BY due_at, id LIMIT ? OFFSET ?", (*params, limit, offset),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def list_due(self, now: datetime, *, limit: int = 100) -> list[Reminder]:
        rows = self._conn.execute(
            "SELECT " + _COLUMNS + " FROM reminders WHERE status='pending' AND due_at<=? "
            "ORDER BY due_at, id LIMIT ?", (now.astimezone(UTC).isoformat(), limit),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def transition(
        self, reminder_id: str, status: ReminderStatus, completed_at: datetime,
    ) -> bool:
        return self._conn.execute(
            "UPDATE reminders SET status=?, completed_at=? WHERE id=? AND status='pending'",
            (status.value, completed_at.isoformat(), reminder_id),
        ).rowcount == 1

    def advance(
        self, reminder_id: str, expected_time: datetime, next_time: datetime,
    ) -> bool:
        return self._conn.execute(
            "UPDATE reminders SET target_time=?, due_at=? "
            "WHERE id=? AND status='pending' AND due_at=?",
            (next_time.isoformat(), next_time.astimezone(UTC).isoformat(), reminder_id,
             expected_time.astimezone(UTC).isoformat()),
        ).rowcount == 1

    def _decode(self, row: tuple[Any, ...]) -> Reminder:
        details = json.loads(self._key.decrypt(row[8])) if row[8] else {}
        recurrence = details.get("recurrence")
        return Reminder(
            id=row[0], conversation_id=row[1], branch_id=row[2],
            target_time=datetime.fromisoformat(row[3]), reason=self._key.decrypt(row[4]),
            created_at=datetime.fromisoformat(row[5]), status=ReminderStatus(row[6]),
            completed_at=datetime.fromisoformat(row[7]) if row[7] else None,
            recurrence=ReminderRecurrence(**recurrence) if recurrence else None,
            source_message_id=details.get("source_message_id"),
        )
