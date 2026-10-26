"""Persistent one-shot reminders; no model or OS wakeup is implied by scheduling."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.reminder import Reminder, ReminderContext, ReminderStatus

REMINDER_TOOLS = frozenset({"set_reminder", "list_reminders", "cancel_reminder"})
DELIVERY_NOTICE = (
    "由应用本地定时器向原会话分支发送系统提醒，不会自动调用模型或执行命令。"
    "需要应用保持运行且资料库已解锁；关闭或休眠期间不能准点推送，恢复后补发。"
)
MAX_PENDING = 100


def validate_reminder_params(tool: str, params: dict[str, Any]) -> None:
    """Semantic validation is shared by the pre-approval gate and execution."""
    if tool == "set_reminder":
        reason = params.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValueError("reason 必须是 1–2000 字的非空提醒内容")
        # Syntax only here: execution checks the live clock after any user approval.
        resolve_target(params.get("target_time"), params.get("delay_seconds"),
                       datetime.now(UTC), require_future=False)
    elif tool == "list_reminders":
        if params.get("status", "pending") not in {"pending", "delivered", "cancelled", "all"}:
            raise ValueError("status 必须是 pending / delivered / cancelled / all")
        for name, low, high in (("limit", 1, 50), ("offset", 0, 10000)):
            value = params.get(name, low)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} 必须是 {low}–{high} 的整数")
    elif tool == "cancel_reminder":
        value = params.get("reminder_id")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
            raise ValueError("reminder_id 必须是非空提醒 ID")


def resolve_target(
    target_time: str | None, delay_seconds: float | None, now: datetime,
    *, require_future: bool = True,
) -> datetime:
    if (target_time is None) == (delay_seconds is None):
        raise ValueError("target_time 与 delay_seconds 必须且只能提供一个")
    try:
        if target_time is not None:
            if not isinstance(target_time, str):
                raise ValueError("target_time 必须是带时区偏移的 ISO 8601 时间")
            target = datetime.fromisoformat(target_time)
            if target.tzinfo is None or target.utcoffset() is None:
                raise ValueError("target_time 必须包含时区偏移，例如 +08:00 或 Z")
        else:
            if (type(delay_seconds) not in (int, float) or delay_seconds is None
                    or not math.isfinite(delay_seconds) or delay_seconds < 1):
                raise ValueError("delay_seconds 必须是大于等于 1 的有限秒数")
            target = now + timedelta(seconds=delay_seconds)
        # Normalize once to ensure extreme offsets cannot overflow at persistence time.
        target.astimezone(UTC)
    except (OverflowError, TypeError) as exc:
        raise ValueError("提醒时间超出有效范围") from exc
    if require_future and target <= now:
        raise ValueError("提醒时间必须晚于当前时间；请先获取当前时间并确认时区")
    return target


def _local_time(value: datetime) -> datetime:
    # Convert the instant, not the clock fields. Let the OS apply the local
    # offset for this date (including DST); never add a hardcoded eight hours.
    return value.astimezone()


def reminder_data(reminder: Reminder) -> dict[str, Any]:
    return {
        "id": reminder.id, "conversation_id": reminder.conversation_id,
        "branch_id": reminder.branch_id,
        "target_time": _local_time(reminder.target_time).isoformat(),
        "timezone_source": "device_local_timezone",
        "reason": reminder.reason, "status": reminder.status.value,
        "created_at": _local_time(reminder.created_at).isoformat(),
        "completed_at": (
            _local_time(reminder.completed_at).isoformat() if reminder.completed_at else None
        ),
    }


class ReminderService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()

    def set_reminder(
        self, context: ReminderContext, *, reason: str,
        target_time: str | None = None, delay_seconds: float | None = None,
    ) -> Reminder:
        now = self._clock()
        validate_reminder_params("set_reminder", {
            "reason": reason, "target_time": target_time, "delay_seconds": delay_seconds,
        })
        target = resolve_target(target_time, delay_seconds, now)
        reminder = Reminder(
            f"reminder_{uuid4().hex}", context.conversation_id, context.branch_id,
            target, reason.strip(), now,
        )
        with self._lock, self._factory() as uow:
            branch = uow.branches.get(context.branch_id)
            if (uow.conversations.get(context.conversation_id) is None or branch is None
                    or branch.conversation_id != context.conversation_id):
                raise ValueError("提醒所属会话或分支已失效")
            uow.reminders.add(reminder)
            if len(uow.reminders.list_for_conversation(
                context.conversation_id, limit=MAX_PENDING + 1,
            )) > MAX_PENDING:
                raise ValueError(f"本会话最多保留 {MAX_PENDING} 条待触发提醒，请先取消不需要的提醒")
            uow.commit()
        return reminder

    def list_reminders(
        self, conversation_id: str, *, status: str = "pending", limit: int = 50, offset: int = 0,
    ) -> list[Reminder]:
        validate_reminder_params(
            "list_reminders", {"status": status, "limit": limit, "offset": offset},
        )
        with self._lock, self._factory() as uow:
            return uow.reminders.list_for_conversation(
                conversation_id, status=status, limit=limit, offset=offset,
            )

    def cancel_reminder(self, conversation_id: str, reminder_id: str) -> Reminder:
        validate_reminder_params("cancel_reminder", {"reminder_id": reminder_id})
        with self._lock, self._factory() as uow:
            item = uow.reminders.get(reminder_id)
            if item is None or item.conversation_id != conversation_id:
                raise ValueError("提醒不存在或不属于当前会话")
            if item.status is ReminderStatus.CANCELLED:
                return item
            now = self._clock()
            if not uow.reminders.transition(item.id, ReminderStatus.CANCELLED, now):
                # Re-read after the conditional write: another scheduler may have won.
                current = uow.reminders.get(item.id)
                if current is not None and current.status is ReminderStatus.CANCELLED:
                    return current
                raise ValueError("提醒已送达，不能取消")
            uow.commit()
            return replace(item, status=ReminderStatus.CANCELLED, completed_at=now)

    def deliver_due(self, *, now: datetime | None = None) -> list[Message]:
        now = now or self._clock()
        messages: list[Message] = []
        with self._lock, self._factory() as uow:
            for item in uow.reminders.list_due(now, limit=100):
                # First write claims under SQLite's writer lock. Message and status
                # are committed together, so crashes/retries cannot double-deliver.
                if not uow.reminders.transition(item.id, ReminderStatus.DELIVERED, now):
                    continue
                late = now - item.target_time > timedelta(seconds=30)
                heading = "⏰ 系统定时提醒（延迟补发）" if late else "⏰ 系统定时提醒"
                local_target = _local_time(item.target_time).isoformat(sep=" ", timespec="seconds")
                content = (f"{heading}\n\n{item.reason}\n\n"
                           f"计划时间：{local_target}（设备本地时间）\n\n"
                           "由应用定时器发送，未调用模型或执行任何操作。")
                message = Message(
                    id=f"notice_{item.id}", conversation_id=item.conversation_id,
                    branch_id=item.branch_id, role=MessageRole.ASSISTANT,
                    content=content, created_at=now,
                )
                uow.messages.add(message)
                messages.append(message)
            uow.commit()
        return messages
