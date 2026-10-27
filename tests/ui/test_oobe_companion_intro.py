"""Approved introduction flow; educational actions must never enable real services."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from limbowave.ui.oobe_copy import SCREENS
from limbowave.ui.oobe_demo import OobeDemo


@pytest.fixture
def page(qtbot):
    view = OobeDemo(preview=False)
    qtbot.addWidget(view)
    view.resize(1120, 800)
    view.show()
    yield view
    view.close()


def test_approved_introductions_are_after_model_setup(page):
    assert list(SCREENS)[:3] == ["intro", "welcome", "provider"]
    path = list(SCREENS)
    assert path.index("models") < path.index("success") < path.index("voice_settings")
    assert path.index("voice_settings") < path.index("voice") < path.index("network")
    assert path.index("network") < path.index("done")
    for expected in ("welcome", "provider"):
        page._primary.click()
        assert page.screen_id == expected
    assert not page._primary.isEnabled()
    assert not any("history" in key for key in SCREENS)


def test_escape_reverses_after_model_intro_sequence(page, qtbot):
    page.show_screen("network")
    for expected in ("voice", "voice_settings", "success", "key"):
        qtbot.keyClick(page, Qt.Key.Key_Escape)
        assert page.screen_id == expected
    page.show_screen("welcome")
    qtbot.keyClick(page, Qt.Key.Key_Escape)
    assert page.screen_id == "intro"


def test_voice_trial_is_stopped_when_leaving_or_hiding(page, qtbot):
    page.show_screen("voice")
    trial = page.voice_introduction.tryout
    qtbot.mousePress(trial.hold, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(trial.hold, Qt.MouseButton.LeftButton)
    assert trial.state == "transcribing"
    page.show_screen("network")
    qtbot.wait(750)
    assert not trial.draft.toPlainText()
    assert not trial.transcription.isActive()
    assert not trial.wave.timer.isActive()
    page.show_screen("voice")
    trial.start_recording()
    page.hide()
    assert not trial.wave.timer.isActive()


def test_simulated_permission_does_not_carry_to_next_page(page):
    page.show_screen("network")
    trial = page.network_introduction.tryout
    trial.configured.setChecked(True)
    trial.request_search()
    trial.approve()
    assert trial.state == "snippets"
    page.show_screen("welcome")
    page.show_screen("network")
    assert trial.state == "ready"
    assert trial.permission.isHidden()


def test_configuration_is_optional_and_not_required_to_continue(page):
    assert not page._optional_settings_factories
    page.show_screen("success")
    assert page._primary.text() == "下一步，设置语音"
    page._primary.click()
    assert page.screen_id == "voice_settings"
    assert page._secondary.text() == "跳过设置"
    page._secondary.click()
    assert page.screen_id == "voice"
    assert page._primary.isEnabled()
    page._primary.click()
    assert page.screen_id == "network"
    finished = []
    page.finished.connect(lambda: finished.append(True))
    page._primary.click()
    assert finished == [True]


def test_no_real_features_are_imported_by_tryouts():
    import ast
    from pathlib import Path

    from limbowave.ui import oobe_companion

    source = Path(oobe_companion.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    forbidden = (
        "QtMultimedia",
        "voice_audio",
        "voice_api",
        "voice_controller",
        "web_access",
        "web_search",
        "httpx",
        "tools.demos",
    )
    assert not [name for name in imports if any(word in name for word in forbidden)]


def test_no_mock_save_or_demo_rail_in_real_flow(page):
    assert not page.findChildren(QPushButton, "oobeRailButton")
    page.show_screen("voice")
    assert all("模拟保存" not in button.text() for button in page.findChildren(QPushButton))

@pytest.mark.parametrize("screen", ["voice", "network"])
@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_small_window_large_font_keeps_navigation_and_trial_accessible(qtbot, screen, scale):
    from PySide6.QtCore import QPoint

    from limbowave.ui import theme
    previous_scale = theme.current_font_scale()
    view = None
    try:
        theme.set_font_scale(scale)
        view = OobeDemo(preview=False)
        qtbot.addWidget(view)
        view.resize(900, 700)
        view.show()
        view.show_screen(screen)
        qtbot.wait(450)
        assert view.width() == 900 and view.height() == 700
        controls = (view._primary, view._secondary)
        if screen == "network":
            controls += (view._optional_settings_button,)
        else:
            assert not view._optional_settings_button.isVisible()
        for control in controls:
            assert control.isVisible()
            position = control.mapTo(view, QPoint(0, 0))
            assert position.x() >= 0 and position.x() + control.width() <= view.width()
            assert position.y() >= 0 and position.y() + control.height() <= view.height()
        introduction = (view.voice_introduction if screen == "voice"
                        else view.network_introduction)
        assert introduction.horizontalScrollBar().maximum() == 0
        trial = introduction.tryout
        control = trial.hold if screen == "voice" else trial.search_button
        introduction.ensureWidgetVisible(control)
        qtbot.wait(30)
        assert introduction.viewport().rect().contains(
            control.mapTo(introduction.viewport(), control.rect().center())
        )
    finally:
        if view is not None:
            view.close()
        theme.set_font_scale(previous_scale)


def test_escape_stops_voice_before_navigating_back(page, qtbot):
    page.show_screen("voice")
    trial = page.voice_introduction.tryout
    trial.start_recording()
    trial.finish_recording()
    assert trial.state == "transcribing"
    qtbot.keyClick(page, Qt.Key.Key_Escape)
    assert page.screen_id == "voice"
    assert not trial.transcription.isActive()
    qtbot.keyClick(page, Qt.Key.Key_Escape)
    assert page.screen_id == "voice_settings"


def test_application_deactivation_stops_educational_capture(page, qapp):
    from PySide6.QtCore import QEvent

    page.show_screen("voice")
    trial = page.voice_introduction.tryout
    trial.start_recording()
    assert trial.wave.timer.isActive()
    qapp.sendEvent(qapp, QEvent(QEvent.Type.ApplicationDeactivate))
    assert not trial.wave.timer.isActive()
    assert trial.state == "idle"

