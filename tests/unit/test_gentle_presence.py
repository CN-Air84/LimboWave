from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from limbowave.application.services.gentle_presence import GentlePresenceService
from limbowave.application.services.preferences_service import Preferences, PreferencesService
from limbowave.domain.presence import PresenceSettings


class Clock:
    def __init__(self, wall: datetime | None = None) -> None:
        self.wall = wall or datetime(2026, 10, 26, 12, 0).astimezone()
        self.ticks = 0.0

    def advance(self, seconds: float) -> None:
        self.ticks += seconds
        self.wall += timedelta(seconds=seconds)


def service(tmp_path: Path, clock: Clock, **settings: object) -> GentlePresenceService:
    return GentlePresenceService(
        tmp_path / "presence_state.json",
        PresenceSettings(enabled=True, **settings),
        monotonic=lambda: clock.ticks,
        now=lambda: clock.wall,
    )


def wait(svc: GentlePresenceService, clock: Clock, seconds: int, *, blocked: bool = False):
    cue = None
    for _ in range(seconds // 30):
        clock.advance(30)
        value = svc.poll(blocked=blocked)
        cue = value or cue
    return cue


def test_default_and_legacy_preferences_are_opt_in(tmp_path):
    assert not Preferences().presence.enabled
    assert not Preferences.from_json({"theme": "light"}).presence.enabled
    prefs = PreferencesService(tmp_path / "preferences.json")
    prefs.save(Preferences(presence=PresenceSettings(enabled=True, muted=True, night_time="00:15")))
    loaded = prefs.load()
    assert loaded.presence.enabled and loaded.presence.muted
    assert loaded.presence.night_time == "00:15"
    prefs.save(replace(loaded, theme="dark"))
    assert prefs.load().presence == loaded.presence


@pytest.mark.parametrize("value", ["true", 1, [], {}, None])
def test_invalid_consent_never_enables(value):
    assert not PresenceSettings.from_json({"enabled": value}).enabled


def test_invalid_options_are_safe():
    parsed = PresenceSettings.from_json(
        {
            "idle_minutes": float("inf"),
            "night_time": "99:99",
            "muted": "false",
            "mode": "notification",
        }
    )
    assert parsed.idle_minutes == 60
    assert parsed.night_time == "23:30"
    assert parsed.muted
    assert parsed.mode == "indicator"
    assert not PresenceSettings.from_json([]).enabled


def test_one_cue_per_idle_episode(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 870) is None
    cue = wait(svc, clock, 30)
    assert cue.reason == "idle"
    assert "无需回复" in cue.text
    assert wait(svc, clock, 5 * 3600) is None
    svc.note_activity()
    assert wait(svc, clock, 15 * 60) is not None


@pytest.mark.parametrize("changes", [{"enabled": False}, {"muted": True}, {"idle_enabled": False}])
def test_disabled_and_muted_never_deliver(tmp_path, changes):
    clock = Clock()
    svc = service(tmp_path, clock)
    svc.configure(replace(svc.settings, **changes))
    assert wait(svc, clock, 2 * 3600) is None


def test_busy_and_recent_interaction_suppress(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 3600, blocked=True) is None
    assert wait(svc, clock, 870) is None
    svc.note_activity()
    assert wait(svc, clock, 870) is None
    assert wait(svc, clock, 30) is not None


def test_night_window_crosses_midnight_without_replay(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 23, 50).astimezone())
    svc = service(tmp_path, clock, idle_enabled=False, night_enabled=True, night_time="00:00")
    assert wait(svc, clock, 570) is None
    cue = wait(svc, clock, 30)
    assert cue.reason == "night"
    assert wait(svc, clock, 300) is None
    clock.advance(23 * 3600)
    assert svc.poll() is None  # resume does not catch up


def test_restart_and_sleep_do_not_catch_up_night(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 23, 29).astimezone())
    svc = service(tmp_path, clock, idle_enabled=False, night_enabled=True)
    assert wait(svc, clock, 300) is None
    clock = Clock(datetime(2026, 10, 27, 23, 0).astimezone())
    svc = service(tmp_path, clock, night_enabled=True)
    clock.advance(30 * 60)
    assert svc.poll() is None
    assert wait(svc, clock, 300) is None


def test_cooldown_and_daily_budget_survive_restart(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 900) is not None
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 900) is None
    assert wait(svc, clock, 4 * 3600) is not None
    svc.note_activity()
    assert wait(svc, clock, 5 * 3600) is None
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 900) is None


def test_clock_rollback_and_corrupt_state_are_fail_closed(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 900) is not None
    clock.wall -= timedelta(days=1)
    svc.note_activity()
    assert wait(svc, clock, 5 * 3600) is None
    (tmp_path / "presence_state.json").write_text("{bad", encoding="utf-8")
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 6 * 3600) is None


def test_budget_save_failure_never_displays(tmp_path, monkeypatch):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)

    def fail(*args):
        raise OSError("read-only disk")

    monkeypatch.setattr(svc, "_save_state", fail)
    assert wait(svc, clock, 900) is None
    assert svc.storage_failed


def test_state_contains_no_conversation_data(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    wait(svc, clock, 900)
    data = json.loads((tmp_path / "presence_state.json").read_text(encoding="utf-8"))
    assert set(data) == {"day", "count", "last_delivered", "last_night"}


def test_night_does_not_duplicate_same_idle_episode(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 18, 0).astimezone())
    svc = service(tmp_path, clock, idle_minutes=15, night_enabled=True)
    assert wait(svc, clock, 900) is not None
    assert wait(svc, clock, 6 * 3600) is None


def test_failed_preference_replacement_keeps_previous_consent(tmp_path, monkeypatch):
    prefs = PreferencesService(tmp_path / "preferences.json")
    prefs.save(Preferences(presence=PresenceSettings(enabled=False)))

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        prefs.save(Preferences(presence=PresenceSettings(enabled=True)))
    assert not prefs.load().presence.enabled


def test_suspend_is_detected_even_when_monotonic_clock_excludes_sleep(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 23, 0).astimezone())
    svc = service(tmp_path, clock, idle_minutes=15, night_enabled=True)
    assert wait(svc, clock, 840) is None
    clock.ticks += 30
    clock.wall = datetime(2026, 10, 26, 23, 30).astimezone()
    assert svc.poll() is None
    assert wait(svc, clock, 300) is None


def test_new_day_restores_budget_after_cooldown(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 900) is not None
    svc.note_activity()
    assert wait(svc, clock, 5 * 3600) is not None
    svc.note_activity()
    assert wait(svc, clock, 5 * 3600) is None
    clock.advance(12 * 3600)
    assert svc.poll() is None  # no wake-up delivery
    assert wait(svc, clock, 900) is not None


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"day": "nonsense"},
        {"day": "2026-10-26", "count": True, "last_delivered": 0, "last_night": ""},
        {"day": "2026-10-26", "count": 0, "last_delivered": float("nan"), "last_night": ""},
        {"day": "2026-10-26", "count": -1, "last_delivered": 0, "last_night": ""},
    ],
)
def test_malformed_budget_never_allows_a_restart_burst(tmp_path, data):
    (tmp_path / "presence_state.json").write_text(json.dumps(data), encoding="utf-8")
    clock = Clock()
    svc = service(tmp_path, clock, idle_minutes=15)
    assert wait(svc, clock, 6 * 3600) is None
