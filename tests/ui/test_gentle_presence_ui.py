from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from limbowave.application.services.gentle_presence import GentlePresenceService
from limbowave.application.services.preferences_service import Preferences, PreferencesService
from limbowave.domain.presence import PresenceCue, PresenceSettings
from limbowave.ui.gentle_presence import GentlePresenceController, PresenceSettingsPage
from limbowave.ui.main_window import MainWindow


@pytest.fixture
def setup(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    prefs = PreferencesService(tmp_path / "preferences.json")
    prefs.save(Preferences(presence=PresenceSettings(enabled=True, idle_minutes=15)))
    clock = [0.0]
    wall = datetime(2026, 10, 26, 12).astimezone()
    svc = GentlePresenceService(
        tmp_path / "presence_state.json",
        prefs.load().presence,
        monotonic=lambda: clock[0],
        now=lambda: wall + timedelta(seconds=clock[0]),
    )
    controller = GentlePresenceController(window, prefs, service=svc)
    controller._timer.stop()
    return window, prefs, clock, controller


def trigger(clock, controller):
    for _ in range(30):
        clock[0] += 30
        controller.poll()


def test_greeting_is_transient_not_a_message_and_does_not_take_focus(setup):
    window, _, clock, controller = setup
    window.chat._input.setFocus()
    focus = QApplication.focusWidget()
    rows = list(window.chat._rows)
    trigger(clock, controller)
    bar = window.chat.presence_bar
    assert not bar.isHidden()
    assert "无需回复" in bar._label.text()
    assert QApplication.focusWidget() is focus
    assert window.chat._rows == rows
    assert window.chat.status_text == "就绪"
    bar._expiry.timeout.emit()
    assert bar.isHidden()
    assert not bar._expiry.isActive()


@pytest.mark.parametrize("block", ["busy", "history", "zen", "draft", "unavailable", "full_page"])
def test_blocked_context_never_shows(setup, block):
    window, _, clock, controller = setup
    if block == "busy":
        window.chat.set_busy(True)
    elif block == "history":
        window.chat.set_history_loading(True)
    elif block == "zen":
        window.chat.set_zen_mode(True)
    elif block == "draft":
        window.chat._input.setPlainText("还没发完")
    elif block == "unavailable":
        window.chat.set_available(False)
    else:
        from PySide6.QtWidgets import QWidget

        window.show_full_page(QWidget())
    trigger(clock, controller)
    assert window.chat.presence_bar.isHidden()


def test_typing_and_busy_clear_immediately(setup, qtbot):
    window, _, clock, controller = setup
    trigger(clock, controller)
    assert not window.chat.presence_bar.isHidden()
    window.chat._input.setPlainText("你好")
    assert window.chat.presence_bar.isHidden()
    window.chat._input.clear()
    window.chat.presence_bar.show_cue(PresenceCue("idle", "我在这里。无需回复。"))
    window.chat.set_busy(True)
    assert window.chat.presence_bar.isHidden()


def test_mute_immediately_stops_and_persists(setup, qtbot):
    window, prefs, clock, controller = setup
    trigger(clock, controller)
    qtbot.mouseClick(window.chat.presence_bar._mute, Qt.MouseButton.LeftButton)
    assert window.chat.presence_bar.isHidden()
    assert prefs.load().presence.muted
    assert not controller._timer.isActive()
    trigger(clock, controller)
    assert window.chat.presence_bar.isHidden()


def test_settings_save_preserves_appearance_and_can_unmute(setup, qtbot):
    _, prefs, _, controller = setup
    original = replace(prefs.load(), font_scale=1.3, background_image="wall.png")
    prefs.save(original)
    page = PresenceSettingsPage(prefs)
    qtbot.addWidget(page)
    page.changed.connect(controller.configure)
    page._muted.setChecked(True)
    assert prefs.load().presence.muted
    page._muted.setChecked(False)
    assert not prefs.load().presence.muted
    assert controller._timer.isActive()
    page._idle_minutes.setValue(120)
    page._night_enabled.setChecked(True)
    assert prefs.load().presence.idle_minutes == 120
    assert prefs.load().presence.night_enabled
    assert prefs.load().font_scale == 1.3
    assert prefs.load().background_image == "wall.png"
    page._enabled.setChecked(False)
    assert not controller._timer.isActive()


def test_mute_save_failure_is_still_muted_this_run(setup, monkeypatch):
    window, prefs, clock, controller = setup
    trigger(clock, controller)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(prefs, "save", fail)
    controller.mute()
    assert controller.service.settings.muted
    assert not controller._timer.isActive()
    assert window.chat.presence_bar.isHidden()
    assert controller.error


def test_settings_failure_is_visible_and_fail_closed(setup, qtbot, monkeypatch):
    _, prefs, _, controller = setup
    page = PresenceSettingsPage(prefs)
    qtbot.addWidget(page)
    page.changed.connect(controller.configure)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(prefs, "save", fail)
    page._muted.setChecked(True)
    assert not controller._timer.isActive()
    assert not controller.service.settings.enabled
    assert "未保存" in page._error.text()


def test_hover_is_not_activity_but_window_deactivation_dismisses(setup):
    window, _, clock, controller = setup
    clock[0] = 100
    controller.eventFilter(window.chat, QEvent(QEvent.Type.Enter))
    assert controller.service._last_activity == 0
    window.chat.presence_bar.show_cue(PresenceCue("idle", "无需回复。"))
    controller.eventFilter(window, QEvent(QEvent.Type.WindowDeactivate))
    assert window.chat.presence_bar.isHidden()


def test_close_disposes_timers_and_event_filter(setup):
    window, _, _, controller = setup
    controller.configure(PresenceSettings(enabled=True))
    assert controller._timer.isActive()
    window.close()
    assert not controller._timer.isActive()
    assert controller._closed


def test_indicator_mode_is_static_and_has_no_reply_action(setup):
    window, _, clock, controller = setup
    controller.configure(replace(controller.service.settings, mode="indicator"))
    trigger(clock, controller)
    assert window.chat.presence_bar._label.text() == "安静在场 · 无需回复"
    assert window.chat.presence_bar._expiry.isSingleShot()


def test_floating_permission_panel_suppresses_and_clears(setup):
    from limbowave.ui.floating import FloatingPanel

    window, _, clock, controller = setup
    trigger(clock, controller)
    assert not window.chat.presence_bar.isHidden()
    panel = FloatingPanel(window, "权限确认")
    panel.show()
    assert window.chat.presence_bar.isHidden()
    trigger(clock, controller)
    assert window.chat.presence_bar.isHidden()
    panel.close_panel()


def test_locked_or_resetting_runtime_is_suppressed(setup):
    window, _, clock, controller = setup
    controller._allowed = lambda: False
    trigger(clock, controller)
    assert window.chat.presence_bar.isHidden()


def test_rejected_close_does_not_stop_presence(setup, qtbot):
    window, _, _, controller = setup
    controller.configure(PresenceSettings(enabled=True))
    window.set_close_guard(lambda: False)
    window.close()
    qtbot.wait(1)
    assert not controller._closed
    assert controller._timer.isActive()
    assert not controller._closing
    window.set_close_guard(lambda: True)


def test_plain_hide_for_companion_mode_clears_without_disposing(setup):
    window, _, clock, controller = setup
    trigger(clock, controller)
    window.hide()
    assert window.chat.presence_bar.isHidden()
    assert not controller._closed


def test_disabled_by_default_has_no_polling(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    preferences = PreferencesService(tmp_path / "preferences.json")
    controller = GentlePresenceController(window, preferences)
    assert not controller._timer.isActive()
    assert window.chat.presence_bar.isHidden()


def test_parent_destruction_removes_filter_without_accessing_deleted_window(qtbot, tmp_path):
    from shiboken6 import delete

    window = MainWindow()
    preferences = PreferencesService(tmp_path / "preferences.json")
    controller = GentlePresenceController(window, preferences)
    delete(window)
    QApplication.processEvents()
    assert controller._closed
    assert controller.eventFilter(QApplication.instance(), QEvent(QEvent.Type.User)) is False


def test_navigation_clears_cue_instead_of_redisplaying_it_on_return(setup, qtbot):
    from PySide6.QtWidgets import QWidget

    window, _, clock, controller = setup
    trigger(clock, controller)
    assert not window.chat.presence_bar.isHidden()
    window.show_full_page(QWidget())
    assert window.chat.presence_bar.isHidden()
    window.show_workspace()
    qtbot.wait(300)
    assert window.chat.presence_bar.isHidden()


def test_application_wires_settings_and_shutdown(qtbot, tmp_path, vault_key, monkeypatch):
    import asyncio

    from qasync import QEventLoop

    import limbowave.app as app
    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure import shell
    from limbowave.ui.settings_panel import SettingsPage

    window = MainWindow()
    qtbot.addWidget(window)
    # Isolate this wiring test from the independent reminder scheduler's I/O loop.
    from limbowave.application.services.reminder_scheduler import ReminderScheduler

    async def no_reminder_io(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(ReminderScheduler, "run", no_reminder_io)
    monkeypatch.setattr(app, "_build_kernel", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *args: None)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    shutdown = None
    try:
        _, _, warm, _, shutdown = app._wire(
            window,
            AppPaths(tmp_path, tmp_path / "logs"),
            vault_key,
            appearance_prepared=True,
        )
        controller = window.findChild(GentlePresenceController)
        assert controller is not None
        assert not controller._timer.isActive()
        warm()
        page = window.findChild(SettingsPage)
        assert page is not None and page.presence_tab is not None
        assert page._rail.index_of("低压在场") >= 0
        page.presence_tab._enabled.setChecked(True)
        assert controller.service.settings.enabled
        assert controller._timer.isActive()
        controller.mute()
        assert page.presence_tab._muted.isChecked()
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.run_until_complete(shutdown())
        shutdown = None
        assert controller._closed
        assert not controller._timer.isActive()
    finally:
        if shutdown is not None:
            loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)
