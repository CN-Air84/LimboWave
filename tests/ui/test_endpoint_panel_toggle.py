"""Regression: the endpoint trigger must toggle, not close on press and reopen on click."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QWidget
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from limbowave.ui.floating import FloatingPanel
from limbowave.ui.main_window import MainWindow
from limbowave.ui.session_toolbar import RouteCandidate


def _open_advanced(qtbot: QtBot) -> MainWindow:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.chat._advanced_btn.click()
    qtbot.waitUntil(lambda: window.chat.advanced_progress == 1.0)
    return window


def _activate(qtbot: QtBot, button: QPushButton, activation: str) -> None:
    if activation == "mouse":
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    elif activation == "keyboard":
        button.setFocus()
        qtbot.keyClick(button, Qt.Key.Key_Space)
    else:
        button.click()


@pytest.mark.parametrize("activation", ["mouse", "keyboard", "programmatic"])
@pytest.mark.parametrize("settled", [False, True])
def test_endpoint_trigger_toggles_and_reopens(
    qtbot: QtBot, activation: str, settled: bool,
) -> None:
    window = _open_advanced(qtbot)
    bar = window.toolbar
    overrides = QSignalSpy(bar.endpoint_override_requested)
    _activate(qtbot, bar._endpoint_value, activation)
    panel = bar._endpoint_panel
    assert panel is not None and panel.isVisible()
    closed = QSignalSpy(panel.closed)
    destroyed = QSignalSpy(panel.destroyed)
    if settled:
        qtbot.waitUntil(lambda: panel._motion is None)

    _activate(qtbot, bar._endpoint_value, activation)
    assert bar._endpoint_panel is None
    assert closed.count() == 1
    assert not bar.collapsed

    # Reopening during the old panel's exit animation must not lose the new reference.
    _activate(qtbot, bar._endpoint_value, activation)
    reopened = bar._endpoint_panel
    assert reopened is not None and reopened is not panel and reopened.isVisible()
    qtbot.waitUntil(lambda: destroyed.count() == 1)
    assert bar._endpoint_panel is reopened
    assert not bar.collapsed
    assert overrides.count() == 0
    reopened.close_panel()


def test_endpoint_trigger_dismisses_on_release_not_press(qtbot: QtBot) -> None:
    window = _open_advanced(qtbot)
    bar = window.toolbar
    qtbot.mouseClick(bar._endpoint_value, Qt.MouseButton.LeftButton)
    panel = bar._endpoint_panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel._motion is None)
    closed = QSignalSpy(panel.closed)

    # The outside-click filter must leave the panel intact until clicked is emitted.
    qtbot.mousePress(bar._endpoint_value, Qt.MouseButton.LeftButton)
    assert bar._endpoint_panel is panel
    assert not panel._closing
    assert closed.count() == 0

    qtbot.mouseRelease(bar._endpoint_value, Qt.MouseButton.LeftButton)
    assert bar._endpoint_panel is None
    assert closed.count() == 1
    assert not bar.collapsed


@pytest.mark.parametrize("dismissal", ["outside", "escape", "close_button"])
def test_endpoint_can_reopen_after_other_dismissals(qtbot: QtBot, dismissal: str) -> None:
    window = _open_advanced(qtbot)
    bar = window.toolbar
    qtbot.mouseClick(bar._endpoint_value, Qt.MouseButton.LeftButton)
    panel = bar._endpoint_panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel._motion is None)
    if dismissal == "outside":
        qtbot.mouseClick(window.chat._input.viewport(), Qt.MouseButton.LeftButton)
        # The advanced area may stay open or collapse depending on its layout.
        # Only the endpoint popup's dismissal belongs to this regression.
        if bar.collapsed:
            window.chat._advanced_btn.click()
            qtbot.waitUntil(lambda: window.chat.advanced_progress == 1.0)
    elif dismissal == "escape":
        combo = panel.findChild(QComboBox)
        assert combo is not None
        qtbot.keyClick(combo, Qt.Key.Key_Escape)
    else:
        close = panel.findChild(QPushButton)
        assert close is not None
        qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert bar._endpoint_panel is None
    assert not bar.collapsed
    qtbot.mouseClick(bar._endpoint_value, Qt.MouseButton.LeftButton)
    reopened = bar._endpoint_panel
    assert reopened is not None and reopened.isVisible()
    reopened.close_panel()


def test_endpoint_toggle_closes_owned_dropdown(qtbot: QtBot) -> None:
    window = _open_advanced(qtbot)
    bar = window.toolbar
    bar.set_route_candidates([
        RouteCandidate("a", "Site A", "model", "Default", current=True),
        RouteCandidate("b", "Site B", "model", "Backup"),
    ])
    bar._endpoint_value.click()
    panel = bar._endpoint_panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel._motion is None)
    combo = panel.findChild(QComboBox)
    assert combo is not None
    combo.showPopup()
    popup = combo.view().window()
    qtbot.waitUntil(popup.isVisible)
    bar._endpoint_value.click()
    assert bar._endpoint_panel is None
    assert not popup.isVisible()
    assert QApplication.activePopupWidget() is None
    assert not bar.collapsed


@pytest.mark.parametrize("anchor_is_toggle", [False, True])
def test_toggle_anchor_opt_in_preserves_other_outside_clicks(
    qtbot: QtBot, anchor_is_toggle: bool,
) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(1000, 800)
    anchor = QPushButton("Anchor", host)
    anchor.setGeometry(20, 20, 180, 36)
    host.show()
    panel = FloatingPanel(host, "Panel")
    closed = QSignalSpy(panel.closed)
    if anchor_is_toggle:
        panel.popup(anchor, anchor_is_toggle=True)
    else:
        panel.popup(anchor)  # Existing callers still dismiss on anchor press by default.
    qtbot.mouseClick(anchor, Qt.MouseButton.LeftButton)
    assert closed.count() == (0 if anchor_is_toggle else 1)
    qtbot.mouseClick(host, Qt.MouseButton.LeftButton)
    assert closed.count() == 1


def test_endpoint_can_recover_a_stale_panel_reference(qtbot: QtBot) -> None:
    window = _open_advanced(qtbot)
    bar = window.toolbar
    bar._endpoint_value.click()
    panel = bar._endpoint_panel
    assert panel is not None
    shadow = panel._shadow
    panel.deleteLater()  # Simulate destruction without the normal closed signal.
    QApplication.sendPostedEvents(panel, QEvent.Type.DeferredDelete)
    assert not isValid(panel)
    # The sibling shadow can outlive the panel until its own deferred deletion.
    # It must stop painting immediately, rather than access the deleted opacity effect.
    assert not isValid(shadow) or not shadow.isVisible()
    assert bar._endpoint_panel is panel
    qtbot.mouseClick(bar._endpoint_value, Qt.MouseButton.LeftButton)
    reopened = bar._endpoint_panel
    assert reopened is not None and reopened is not panel and reopened.isVisible()
    reopened.close_panel()
