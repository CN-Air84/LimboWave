"""Isolated acceptance checks for the unshipped onboarding proposal."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt

DEMO_PATH = Path(__file__).with_name("voice_oobe_demo.py")
_spec = importlib.util.spec_from_file_location("voice_oobe_acceptance_demo", DEMO_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.fixture
def demo(qtbot):
    window = _module.VoiceOobeDemo()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)
    yield window
    window.tryout.stop_all()
    window.close()


def test_offline_intro_coexists_with_approved_production_screens(demo):
    from limbowave.ui.oobe_copy import SCREENS

    assert demo.screen_id == "voice"
    assert "voice" in SCREENS
    assert "history" not in SCREENS
    assert "history" not in demo.indices
    assert not hasattr(demo, "history_tryout")
    from PySide6.QtWidgets import QLabel, QPushButton

    visible_copy = [w.text() for w in [*demo.findChildren(QLabel), *demo.findChildren(QPushButton)]]
    assert all("前后文" not in text and "接上话题" not in text for text in visible_copy)
    assert demo.tryout.state_label.text() == "等你按住按钮"
    assert demo.tryout.message.isHidden()
    assert not demo.tryout.send.isEnabled()


@pytest.mark.parametrize("input_method", ["mouse", "keyboard"])
def test_review_then_explicit_send(demo, qtbot, input_method):
    trial = demo.tryout
    if input_method == "mouse":
        qtbot.mousePress(trial.hold, Qt.MouseButton.LeftButton)
    else:
        qtbot.keyPress(trial.hold, Qt.Key.Key_Space)
    assert trial.state == "recording"
    assert trial.wave.timer.isActive()
    if input_method == "mouse":
        qtbot.mouseRelease(trial.hold, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
    else:
        qtbot.keyRelease(trial.hold, Qt.Key.Key_Space)
    assert trial.state == "transcribing"
    assert trial.message.isHidden()
    qtbot.waitUntil(lambda: trial.state == "review")
    assert trial.message.isHidden()
    assert trial.send.isEnabled()
    trial.draft.setPlainText("我校对过的文字")
    qtbot.mouseClick(trial.send, Qt.MouseButton.LeftButton)
    assert trial.state == "sent"
    assert trial.message.text() == "我校对过的文字"
    qtbot.mouseClick(trial.read, Qt.MouseButton.LeftButton)
    assert trial.state == "playing"
    qtbot.keyClick(demo, Qt.Key.Key_Escape)
    assert trial.state == "sent"
    assert not trial.wave.timer.isActive()
    assert not trial.playback.isActive()


def test_cancelling_transcription_keeps_draft(demo, qtbot):
    trial = demo.tryout
    trial.start_recording()
    trial.finish_recording()
    trial.stop_all()
    qtbot.wait(750)
    assert trial.draft.toPlainText() == ""
    assert trial.state == "idle"


def test_switching_screen_cancels_active_demo(demo, qtbot):
    trial = demo.tryout
    trial.start_recording()
    trial.finish_recording()
    demo.show_screen("setup")
    qtbot.wait(750)
    assert trial.draft.toPlainText() == ""
    assert not trial.transcription.isActive()
    assert not trial.wave.timer.isActive()


def test_config_is_fake_and_key_is_discarded(demo, qtbot):
    demo.show_screen("setup")
    fields = demo.fields["stt"]
    fields["key"].setText("not-a-real-key")
    fields["mode"].setCurrentIndex(1)
    assert fields["key"].text() == ""
    assert "127.0.0.1" in fields["url"].placeholderText()
    fields["key"].setText("also-fake")
    fields["enabled"].setChecked(True)
    demo.mock_save()
    assert fields["key"].text() == ""
    assert demo.temporary_config == {
        "stt": {"enabled": True, "local": True},
        "tts": {"enabled": False, "local": False},
    }
    assert demo.screen_id == "voice"
    assert "正式设置未改动" in demo.footer_note.text()


def test_navigation_and_themes(demo, qtbot):
    from limbowave.ui import theme

    initial = theme.current_palette().name
    demo.toggle_theme()
    assert theme.current_palette().name != initial
    demo.toggle_theme()
    qtbot.mouseClick(demo.continue_button, Qt.MouseButton.LeftButton)
    assert demo.screen_id == "network"
    qtbot.mouseClick(demo.network_continue_button, Qt.MouseButton.LeftButton)
    assert demo.screen_id == "welcome"
    qtbot.keyClick(demo, Qt.Key.Key_Escape)
    assert demo.screen_id == "network"
    qtbot.keyClick(demo, Qt.Key.Key_Escape)
    assert demo.screen_id == "voice"
    demo.resize(1000, 760)
    qtbot.wait(30)
    assert demo.continue_button.isVisible()
    assert (
        demo.continue_button.mapTo(demo, QPoint()).y() + demo.continue_button.height()
        < demo.height()
    )


def test_demo_imports_no_audio_network_or_persistence():
    forbidden = (
        "httpx",
        "socket",
        "requests",
        "QtMultimedia",
        "voice_service",
        "voice_api",
        "voice_controller",
        "credential_service",
        "secret_store",
        "vault",
        "chat_history_service",
        "chat_history_context",
        "history_reader",
        "sqlite",
        "repositories",
        "oobe_live",
    )
    tree = ast.parse(DEMO_PATH.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [name for name in names if any(term in name for term in forbidden)]












def test_network_trial_defaults_off_and_does_not_mutate_production_copy(demo):
    from limbowave.ui.oobe_copy import SCREENS

    original_copy = dict(SCREENS)
    demo.show_screen("network")
    trial = demo.network_tryout
    assert not trial.configured.isChecked()
    trial.request_search()
    assert trial.state == "unconfigured" and trial.evidence.isHidden()
    assert "未配置" in trial.status.text()
    assert original_copy == SCREENS
    assert "network" in SCREENS


def test_network_search_and_page_are_separate_consents(demo):
    demo.show_screen("network")
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    assert trial.state == "search_permission" and not trial.permission.isHidden()
    assert trial.evidence.isHidden()
    assert "SerpAPI" in trial.permission_text.text()
    trial.approve()
    assert trial.state == "snippets" and "搜索到" in trial.evidence.text()
    assert "摘要" in trial.status.text() and "查阅了这篇" not in trial.evidence.text()
    trial.request_page()
    assert trial.state == "page_permission" and trial.evidence.isHidden()
    trial.approve()
    assert trial.state == "page_content" and "查阅了这篇" in trial.evidence.text()
    assert "虚构演示" in trial.evidence.text()


def test_network_denial_failure_and_departure_do_not_keep_authorization(demo, qtbot):
    demo.show_screen("network")
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    qtbot.keyClick(demo, Qt.Key.Key_Escape)
    assert trial.state == "denied" and trial.evidence.isHidden()
    trial.approve()
    assert trial.state == "denied"
    trial.fail()
    assert trial.state == "failed" and "不能据此判断" in trial.status.text()
    trial.request_search()
    trial.approve()
    demo.show_screen("voice")
    demo.show_screen("network")
    assert trial.state == "ready" and trial.evidence.isHidden()
    trial.request_page()
    assert trial.state == "ready"


def test_network_provider_change_invalidates_simulated_consent(demo):
    demo.show_screen("network")
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    trial.approve()
    trial.provider.setCurrentIndex(2)
    assert trial.state == "ready" and trial.evidence.isHidden()
    trial.request_search()
    assert "Tavily" in trial.permission_text.text()


def test_network_small_window_keeps_continue_visible(demo, qtbot):
    from PySide6.QtCore import QPoint

    demo.show_screen("network")
    demo.resize(1000, 760)
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    trial.approve()
    trial.request_page()
    trial.approve()
    qtbot.wait(50)
    assert demo.network_continue_button.isVisible()
    assert (
        demo.network_continue_button.mapTo(demo, QPoint()).y()
        + demo.network_continue_button.height()
        < demo.height()
    )
