"""Integration checks for the paired companion / Zen entry."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QWidget

from limbowave.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    return window


@pytest.mark.parametrize("zen", [False, True])
def test_capsule_opens_mini_and_preserves_zen_and_draft(window, qtbot, zen):
    chat = window.chat
    chat.set_zen_mode(zen)
    chat._input.setPlainText("同一份草稿")
    capsule = chat._mode_capsule
    assert capsule.companion_button is chat._companion_btn
    assert capsule.zen_button is chat._zen_btn
    assert not chat._companion_btn.isCheckable()
    qtbot.mouseClick(chat._companion_btn, Qt.MouseButton.LeftButton)
    mini = window._companion.window
    assert mini is not None and mini.isVisible()
    assert not window.isVisible()
    assert mini._input.document() is chat._input.document()
    mini.close()
    assert window.isVisible()
    assert chat.zen_mode is zen
    assert chat._zen_btn.isChecked() is zen
    assert chat._input.toPlainText() == "同一份草稿"
    assert not any(button.text() == "迷你陪伴"
                   for button in window.sidebar.findChildren(QPushButton))


def test_history_capsule_uses_preview_not_live_draft(window, qtbot):
    window.chat._input.setPlainText("实时草稿")
    for _ in range(2):
        preview = window.prepare_history_preview()
        preview._input.setPlainText("历史草稿")
        window.show_history_preview()
        qtbot.mouseClick(preview._companion_btn, Qt.MouseButton.LeftButton)
        mini = window._companion.window
        assert mini is not None and mini.isVisible()
        assert mini._input.document() is preview._input.document()
        assert mini._input.toPlainText() == "历史草稿"
        mini.close()
        window.clear_history_preview()
    assert window.chat._input.toPlainText() == "实时草稿"


def test_capsule_keeps_modal_and_full_page_entry_guards(window):
    from PySide6.QtWidgets import QDialog

    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    window.chat._companion_btn.click()
    assert not window._companion.active
    dialog.close()
    window.show_full_page(QWidget())
    window.chat._companion_btn.click()
    assert not window._companion.active
