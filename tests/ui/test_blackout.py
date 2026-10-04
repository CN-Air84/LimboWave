"""Black veil fades without sliding, skipping frames or uncovering on resize."""

from __future__ import annotations

import time

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from limbowave.ui.blackout import Blackout
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow


def test_veil_fades_over_stationary_content_and_resizes(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(400, 300)
    palette = parent.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    parent.setPalette(palette)
    parent.setAutoFillBackground(True)
    parent.show()
    veil = Blackout(parent)
    veil.fade_to(1.0, veil.FADE_IN_MS)
    assert parent.grab().toImage().pixelColor(20, 20).red() == 255
    qtbot.waitUntil(lambda: 0.2 < veil.opacity < 0.8, timeout=600)
    assert 0 < parent.grab().toImage().pixelColor(20, 20).red() < 255
    qtbot.waitUntil(lambda: veil.opacity == 1, timeout=600)
    assert parent.grab().toImage().pixelColor(20, 20).red() == 0
    veil.fade_to(0.0, veil.FADE_OUT_MS)
    parent.resize(500, 360)
    assert veil.geometry() == parent.rect()
    qtbot.waitUntil(lambda: 0.2 < veil.opacity < 0.8, timeout=600)
    assert 0 < parent.grab().toImage().pixelColor(20, 20).red() < 255
    qtbot.waitUntil(lambda: veil.opacity == 0, timeout=600)
    assert parent.grab().toImage().pixelColor(20, 20).red() == 255


def test_veil_does_not_skip_after_a_stall_or_hide(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    veil = Blackout(parent)
    veil.fade_to(1.0, veil.FADE_IN_MS)
    time.sleep(0.15)
    veil._advance()
    assert veil._elapsed_ms <= 32
    opacity = veil.opacity
    parent.hide()
    assert not veil._timer.isActive()
    parent.show()
    assert veil.opacity == opacity
    assert veil._timer.isActive()


def test_workspace_appears_under_opaque_veil_after_logo_erasure(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()
    window.show_workspace()
    boundary: list[tuple[float, float]] = []
    page.exit_ready.connect(
        lambda: boundary.append((page._logo._erasure, window._login_fade.opacity))
    )
    qtbot.waitUntil(lambda: window._login_fade is not None, timeout=4000)
    assert boundary == [(1.0, 1.0)]
    assert window.transition_active
    veil = window._login_fade
    qtbot.waitUntil(lambda: 0.2 < veil.opacity < 0.8, timeout=600)
    assert window._transition is None
    position = window._workspace.pos()
    window.resize(window.width() + 80, window.height() + 50)
    assert veil.geometry() == window._root_stack.rect()
    assert window._workspace.pos() == position
    assert window.transition_active
    qtbot.waitUntil(lambda: not window.transition_active, timeout=1000)
    assert window._login_fade is None
