"""Small calendar recurrence rules; instants are compared in UTC, not wall time."""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from limbowave.domain.reminder import ReminderRecurrence

FREQUENCIES = {"daily", "weekdays", "weekly"}
LABELS = {"daily": "每天", "weekdays": "周一至周五", "weekly": "每周"}


def resolve_recurrence(
    frequency: str | None, timezone: str | None, target: datetime,
) -> ReminderRecurrence | None:
    if frequency is None:
        if timezone is not None:
            raise ValueError("timezone 仅用于周期提醒，需同时提供 recurrence")
        return None
    if not isinstance(frequency, str) or frequency not in FREQUENCIES:
        raise ValueError("recurrence 必须是 daily / weekdays / weekly")
    if not isinstance(timezone, str) or not timezone or len(timezone) > 100:
        raise ValueError("周期提醒必须提供 IANA timezone，例如 Asia/Shanghai")
    try:
        zone = ZoneInfo(timezone)
        local = target.astimezone(zone)
    except (ZoneInfoNotFoundError, ValueError, OverflowError) as exc:
        raise ValueError("timezone 必须是有效的 IANA 时区") from exc
    if target.utcoffset() != local.utcoffset():
        raise ValueError("target_time 的时区偏移与 timezone 不一致，请确认首次提醒的本地时间")
    if frequency == "weekdays" and local.weekday() >= 5:
        raise ValueError("weekdays 的首次 target_time 必须是周一至周五")
    rule = ReminderRecurrence(frequency, timezone, local.time().isoformat(), local.weekday())
    next_occurrence(rule, target)  # Reject schedules which cannot advance (datetime overflow).
    return rule


def next_occurrence(rule: ReminderRecurrence | None, after: datetime) -> datetime:
    """Next local-calendar occurrence strictly after an instant; no catch-up loop.

    An ambiguous hour fires once (fold=0). A nonexistent hour shifts forward by
    the DST gap on that date only; the stored local clock remains unchanged.
    """
    if rule is None:
        raise ValueError("一次性提醒没有下一次周期")
    zone = ZoneInfo(rule.timezone)
    clock = time.fromisoformat(rule.local_time)
    after_utc = after.astimezone(UTC)
    day = after_utc.astimezone(zone).date()
    try:
        for _ in range(8):
            eligible = (rule.frequency == "daily"
                        or (rule.frequency == "weekdays" and day.weekday() < 5)
                        or (rule.frequency == "weekly" and day.weekday() == rule.weekday))
            if eligible:
                wall = datetime.combine(day, clock, tzinfo=zone).replace(fold=0)
                # UTC round-trip resolves nonexistent wall times forward, and
                # the UTC comparison avoids fold-related datetime equality.
                candidate = wall.astimezone(UTC)
                if candidate > after_utc:
                    return candidate
            day += timedelta(days=1)
    except OverflowError as exc:
        raise ValueError("周期提醒时间超出有效范围") from exc
    raise ValueError("无法计算下一次周期提醒")
