from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReminderNotificationSettings:
    """Only explicitly scheduled reminders; independent of low-pressure presence."""

    sound_enabled: bool = True
    desktop_enabled: bool = True

    @classmethod
    def from_json(cls, value: object) -> "ReminderNotificationSettings":
        data = value if isinstance(value, dict) else {}
        defaults = cls()
        return cls(**{
            key: data[key] if type(data.get(key)) is bool else getattr(defaults, key)
            for key in ("sound_enabled", "desktop_enabled")
        })
