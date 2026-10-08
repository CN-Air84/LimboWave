"""The debug shortcut is window-wide, including full-page OOBE and focused fields."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QLineEdit
from pytestqt.qtbot import QtBot

from limbowave.ui.main_window import MainWindow


@pytest.mark.parametrize("full_page", [False, True])
def test_oobe_restart_shortcut_with_child_focus(qtbot: QtBot, full_page: bool) -> None:
    window = MainWindow(enable_oobe_debug=True)
    qtbot.addWidget(window)
    field = QLineEdit(window)
    if full_page:
        window.show_full_page(field)
    window.show()
    window.activateWindow()
    field.show()
    field.setFocus()
    qtbot.waitUntil(window.isActiveWindow)
    action = next(
        action for action in window.actions() if action.shortcut() == QKeySequence("Ctrl+Shift+O")
    )
    assert not action.autoRepeat()
    with qtbot.waitSignal(window.oobe_restart_requested):
        qtbot.keyClick(
            field,
            Qt.Key.Key_O,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )


def test_oobe_shortcut_is_not_registered_by_default(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert not any(action.shortcut() == QKeySequence("Ctrl+Shift+O") for action in window.actions())
