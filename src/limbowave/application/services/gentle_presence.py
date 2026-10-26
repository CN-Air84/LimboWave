"""Rate-limited, local-only presence. No model, messages, OS input hooks or notifications."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path

from limbowave.domain.presence import PresenceCue, PresenceSettings

DAILY_LIMIT = 2
COOLDOWN_SECONDS = 4 * 3600
NIGHT_WINDOW_SECONDS = 5 * 60
RESUME_GAP_SECONDS = 90


@dataclass(frozen=True, slots=True)
class _DeliveryState:
    day: str = ""
    count: int = 0
    last_delivered: float = 0.0
    last_night: str = ""


class GentlePresenceService:
    """Decide and durably reserve a cue before the UI may show it.

    Monotonic time measures in-app inactivity; local wall time defines the night
    window and persisted cooldown. A backwards wall clock therefore suppresses
    delivery instead of resetting the budget. Blocked/wake states never catch up.
    """

    def __init__(
        self,
        path: Path,
        settings: PresenceSettings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._path = path
        self._monotonic = monotonic
        self._now = now
        self.settings = PresenceSettings.from_json(asdict(settings))
        self._last_activity = monotonic()
        self._last_poll = self._last_activity
        self._last_wall = now().timestamp()
        self._idle_delivered = False
        self._night_seen = self._night_key(now())
        self.storage_failed = False
        self._state = self._load_state()

    def _load_state(self) -> _DeliveryState:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid state")
            day, count = data["day"], data["count"]
            last, night = data["last_delivered"], data["last_night"]
            if not isinstance(day, str) or not isinstance(night, str):
                raise ValueError("invalid dates")
            date.fromisoformat(day)
            if night:
                date.fromisoformat(night)
            if type(count) is not int or not 0 <= count <= DAILY_LIMIT:
                raise ValueError("invalid budget")
            if type(last) not in (int, float) or not math.isfinite(last) or last < 0:
                raise ValueError("invalid timestamp")
            return _DeliveryState(day, count, float(last), night)
        except FileNotFoundError:
            return _DeliveryState()
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            # Broken records must not turn a restart into another greeting today.
            return _DeliveryState(
                self._now().date().isoformat(), DAILY_LIMIT, self._now().timestamp()
            )

    def _save_state(self, state: _DeliveryState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state)) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    def configure(self, settings: PresenceSettings) -> None:
        sanitized = PresenceSettings.from_json(asdict(settings))
        if sanitized == self.settings:
            return
        self.settings = sanitized
        self.note_activity()
        self._last_poll = self._monotonic()
        self._last_wall = self._now().timestamp()
        self._night_seen = self._night_key(self._now())

    def note_activity(self) -> None:
        self._last_activity = self._monotonic()
        self._idle_delivered = False
        # An interaction during the night window cancels it, not delays it.
        night = self._night_key(self._now())
        if night:
            self._night_seen = night

    def _night_key(self, now: datetime) -> str:
        hour, minute = map(int, self.settings.night_time.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elapsed = (now - start).total_seconds()
        return now.date().isoformat() if 0 <= elapsed < NIGHT_WINDOW_SECONDS else ""

    def poll(self, *, blocked: bool = False) -> PresenceCue | None:
        ticks, now = self._monotonic(), self._now()
        gap = ticks - self._last_poll
        wall_gap = now.timestamp() - self._last_wall
        self._last_poll = ticks
        self._last_wall = now.timestamp()
        if (
            blocked
            or gap > RESUME_GAP_SECONDS
            or gap < 0
            or wall_gap > RESUME_GAP_SECONDS
            or wall_gap < 0
            or not self.settings.enabled
            or self.settings.muted
        ):
            self.note_activity()
            return None
        if self.storage_failed:
            return None
        quiet = ticks - self._last_activity
        night = self._night_key(now)
        reason = ""
        if (
            not self._idle_delivered
            and self.settings.night_enabled
            and night
            and night != self._night_seen
            and night != self._state.last_night
        ):
            self._night_seen = night
            if quiet >= NIGHT_WINDOW_SECONDS:
                reason = "night"
        if (
            not reason
            and self.settings.idle_enabled
            and not self._idle_delivered
            and quiet >= self.settings.idle_minutes * 60
        ):
            reason = "idle"
        if not reason:
            return None
        day = now.date().isoformat()
        if self._state.day > day:
            return None
        count = self._state.count if self._state.day == day else 0
        if count >= DAILY_LIMIT or (
            self._state.last_delivered
            and now.timestamp() - self._state.last_delivered < COOLDOWN_SECONDS
        ):
            return None
        updated = replace(
            self._state,
            day=day,
            count=count + 1,
            last_delivered=now.timestamp(),
            last_night=night if reason == "night" else self._state.last_night,
        )
        try:
            self._save_state(updated)
        except OSError:
            self.storage_failed = True
            return None
        self._state = updated
        # Either trigger counts as the single cue in this inactive episode.
        self._idle_delivered = True
        text = (
            "夜深了，愿你有一段安静的时光。无需回复。"
            if reason == "night"
            else "我在这里。你可以按自己的节奏来，无需回复。"
        )
        return PresenceCue(reason, text)
