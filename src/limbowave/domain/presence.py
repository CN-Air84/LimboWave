"""Local opt-in controls for gentle presence; not conversation or model settings."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PresenceSettings:
    enabled: bool = False
    muted: bool = False
    idle_enabled: bool = True
    idle_minutes: int = 60
    night_enabled: bool = False
    night_time: str = "23:30"
    mode: str = "greeting"

    @classmethod
    def from_json(cls, data: object) -> PresenceSettings:
        if not isinstance(data, dict):
            return cls()
        minutes = data.get("idle_minutes", 60)
        # Do not coerce booleans, strings or non-finite numbers into consent/timers.
        minutes = max(15, min(240, minutes)) if type(minutes) is int else 60
        night = data.get("night_time", "23:30")
        if not isinstance(night, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", night):
            night = "23:30"
        mode = data.get("mode", "greeting")
        return cls(
            enabled=data.get("enabled") is True,
            muted=data.get("muted", False) is not False,
            idle_enabled=data.get("idle_enabled", True) is True,
            idle_minutes=minutes,
            night_enabled=data.get("night_enabled") is True,
            night_time=night,
            mode=mode if mode in ("greeting", "indicator") else "indicator",
        )


@dataclass(frozen=True, slots=True)
class PresenceCue:
    reason: str
    text: str
