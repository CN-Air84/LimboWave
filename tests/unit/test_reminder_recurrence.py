from datetime import UTC, datetime

import pytest

from limbowave.domain.reminder_recurrence import next_occurrence, resolve_recurrence


def test_weekdays_skip_weekend_and_keep_local_clock():
    first = datetime.fromisoformat("2026-10-30T12:30:00+08:00")
    rule = resolve_recurrence("weekdays", "Asia/Shanghai", first)
    assert next_occurrence(rule, first) == datetime.fromisoformat("2026-11-02T12:30:00+08:00")
    assert next_occurrence(rule, datetime.fromisoformat("2026-11-10T15:00:00+08:00")) == (
        datetime.fromisoformat("2026-11-11T12:30:00+08:00")
    )


def test_weekly_keeps_original_weekday_after_long_absence():
    first = datetime.fromisoformat("2026-10-27T23:00:00+08:00")
    rule = resolve_recurrence("weekly", "Asia/Shanghai", first)
    assert next_occurrence(rule, datetime.fromisoformat("2026-11-12T12:00:00+08:00")) == (
        datetime.fromisoformat("2026-11-17T23:00:00+08:00")
    )


def test_daily_survives_dst_without_repeating_ambiguous_hour():
    rule = resolve_recurrence(
        "daily", "America/New_York", datetime.fromisoformat("2026-10-31T01:30:00-04:00"),
    )
    first = next_occurrence(rule, datetime.fromisoformat("2026-10-31T02:00:00-04:00"))
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(rule, first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_spring_gap_shifts_forward_only_on_the_gap_day():
    rule = resolve_recurrence(
        "daily", "America/New_York", datetime.fromisoformat("2026-03-07T02:30:00-05:00"),
    )
    gap = next_occurrence(rule, datetime.fromisoformat("2026-03-07T03:00:00-05:00"))
    assert gap == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # 03:30 EDT
    assert next_occurrence(rule, gap) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize("kind, zone, target", [
    ("hourly", "Asia/Shanghai", "2026-10-27T12:30:00+08:00"),
    ("daily", None, "2026-10-27T12:30:00+08:00"),
    (None, "Asia/Shanghai", "2026-10-27T12:30:00+08:00"),
    ("daily", "Unknown/Zone", "2026-10-27T12:30:00+08:00"),
    ("daily", "../etc/passwd", "2026-10-27T12:30:00+08:00"),
    ("daily", "Asia/Shanghai", "2026-10-27T12:30:00-04:00"),
    ("weekdays", "Asia/Shanghai", "2026-10-31T12:30:00+08:00"),
])
def test_invalid_rule(kind, zone, target):
    with pytest.raises(ValueError):
        resolve_recurrence(kind, zone, datetime.fromisoformat(target))


def test_no_recurrence_is_backwards_compatible():
    assert resolve_recurrence(None, None, datetime.now(UTC)) is None
