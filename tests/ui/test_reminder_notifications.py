from dataclasses import replace

import pytest
from PySide6.QtWidgets import QWidget

from limbowave.application.services.preferences_service import PreferencesService
from limbowave.domain.reminder_notifications import ReminderNotificationSettings as Settings
from limbowave.ui.reminder_notifications import (
    ReminderNotifications,
    ReminderNotificationSettingsPage,
)


class Sound:
    def __init__(self):
        self.plays = self.stops = 0
    def play(self):
        self.plays += 1
    def stop(self):
        self.stops += 1


class Balloons:
    def __init__(self):
        self.messages = []
        self.closed = 0
    def show(self, title, body, on_click):
        self.messages.append((title, body, on_click))
        return True
    def close(self):
        self.closed += 1


@pytest.fixture
def notifier(qtbot, tmp_path):
    window = QWidget()
    qtbot.addWidget(window)
    sound, balloons = Sound(), Balloons()
    now = [0.0]
    preferences = PreferencesService(tmp_path / "prefs.json")
    controller = ReminderNotifications(
        window, preferences, sound_factory=lambda: sound, balloon_factory=lambda: balloons,
        clock=lambda: now[0],
    )
    yield controller, sound, balloons, now, preferences
    controller.shutdown()


def flush(controller):
    controller._timer.stop()
    controller._flush()


def test_burst_deduplicates_coalesces_and_does_not_publish_private_text(notifier):
    controller, sound, balloons, now, _ = notifier
    assert controller._sound is None and controller._balloons is None
    for i in range(100):
        controller.notify("c", "b", f"m{i}")
    controller.notify("c", "b", "m0")
    flush(controller)
    assert sound.plays == 1 and len(balloons.messages) == 1
    assert "100" in balloons.messages[0][1]
    opened = []
    controller.open_requested.connect(opened.append)
    balloons.messages[0][2]()
    assert opened == [("c", "b", "m99")]
    controller.notify("other", "branch", "later")
    flush(controller)
    assert len(balloons.messages) == 1
    now[0] = 5.0
    flush(controller)
    assert sound.plays == 2 and len(balloons.messages) == 2
    balloons.messages[0][2]()
    assert opened[-1] == ("c", "b", "m99")  # Old callback retains old scope.


def test_timer_actually_flushes(notifier, qtbot):
    controller, sound, balloons, _, _ = notifier
    controller.notify("c", "b", "m")
    qtbot.waitUntil(lambda: len(balloons.messages) == 1, timeout=1500)
    assert sound.plays == 1


def test_settings_muting_pending_effects_and_preserving_preferences(notifier):
    controller, sound, balloons, now, preferences = notifier
    preferences.save(replace(preferences.load(), font_family="Keep Font"))
    controller.configure(Settings(False, True))
    controller.notify("c", "b", "m1")
    flush(controller)
    assert sound.plays == 0 and len(balloons.messages) == 1
    now[0] = 6
    controller.configure(Settings(True, False))
    controller.notify("c", "b", "m2")
    flush(controller)
    assert sound.plays == 1 and balloons.closed == 1
    controller.notify("c", "b", "m3")
    controller.configure(Settings(False, False))
    flush(controller)
    assert sound.plays == 1 and not controller._pending
    assert sound.stops == 1
    assert preferences.load().font_family == "Keep Font"
    assert preferences.load().reminder_notifications == Settings(False, False)
    controller.preview_sound()
    assert sound.plays == 2


def test_shutdown_and_lock_cancel_pending_and_clicks(notifier):
    controller, sound, balloons, now, _ = notifier
    controller.notify("c", "b", "m1")
    flush(controller)
    opened = []
    controller.open_requested.connect(opened.append)
    controller._allowed = lambda: False
    balloons.messages[0][2]()
    assert not opened
    controller._allowed = lambda: True
    controller.notify("c", "b", "m2")
    controller.shutdown()
    now[0] = 10
    flush(controller)
    balloons.messages[0][2]()
    assert not opened and sound.plays == 1 and sound.stops == 1
    assert balloons.closed == 1


def test_failed_backends_do_not_break_delivery_ui(notifier, caplog):
    controller, _, _, _, _ = notifier
    def fail():
        raise RuntimeError("private-reminder-canary")
    controller._sound_factory = fail
    controller._balloon_factory = fail
    controller.notify("c", "b", "m")
    flush(controller)
    assert "private-reminder-canary" not in caplog.text


def test_settings_page_applies_live_and_reports_failed_save(notifier, qtbot, monkeypatch):
    controller, sound, _, _, preferences = notifier
    page = ReminderNotificationSettingsPage(preferences)
    qtbot.addWidget(page)
    page.changed.connect(controller.configure)
    page.preview_requested.connect(controller.preview_sound)
    controller.settings_changed.connect(page.reload)
    controller.error_changed.connect(page.show_error)
    page.sound.setChecked(False)
    assert not preferences.load().reminder_notifications.sound_enabled
    page.preview_requested.emit()
    assert sound.plays == 1
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(preferences, "save", fail)
    page.desktop.setChecked(False)
    assert controller.settings == Settings(False, False)
    assert "未能保存" in page._error.text()

def test_idle_batch_returns_to_short_debounce_after_throttling(notifier):
    controller, sound, balloons, now, _ = notifier
    controller.notify("c", "b", "first")
    flush(controller)
    now[0] = 1
    controller.notify("c", "b", "second")
    flush(controller)
    assert controller._timer.interval() == 4000
    now[0] = 5
    flush(controller)
    now[0] = 30
    controller.notify("c", "b", "third")
    assert controller._timer.interval() == 250
    flush(controller)
    assert sound.plays == len(balloons.messages) == 3
