"""Exercise native show events and mouse-driven popups, not setCurrentIndex shortcuts."""

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QWidget

from limbowave.ui.floating import FloatingPanel
from limbowave.ui.session_memory_panel import MemoryPanel
from tests.unit.test_memory_service import memory_stack as memory_stack


@pytest.mark.parametrize("scope", [(None, None), ("c", "b")])
def test_constructing_memory_panel_never_shows_temporary_top_level_widgets(
    qtbot, qapp, memory_stack, scope,
):
    service, _ = memory_stack
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    outer = FloatingPanel(host, "记忆")
    shown = []

    class ShowObserver(QObject):
        def eventFilter(self, watched, event):
            if (event.type() == QEvent.Type.Show
                    and isinstance(watched, QWidget) and watched.isWindow()):
                shown.append((type(watched).__name__, watched.objectName()))
            return False

    observer = ShowObserver()
    qapp.installEventFilter(observer)
    try:
        panel = MemoryPanel(service, outer, conversation_id=scope[0], branch_id=scope[1])
        outer.content_layout.addWidget(panel)
    finally:
        qapp.removeEventFilter(observer)
    qtbot.waitUntil(panel.isEnabled)
    assert shown == [], f"Controls were briefly shown as separate windows: {shown}"


def test_mouse_scope_popup_keeps_memory_panel_open_and_allows_editing(qtbot, memory_stack):
    service, _ = memory_stack
    original = service.save("全局标题\n原文")
    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    host.show()
    outer = FloatingPanel(host, "会话记忆", width=620)
    panel = MemoryPanel(service, outer, conversation_id="c", branch_id="b")
    outer.content_layout.addWidget(panel)
    outer.popup()
    qtbot.waitUntil(lambda: outer._motion is None and panel.isEnabled())
    tabs = panel._subtabs
    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(1).center())
    qtbot.waitUntil(lambda: panel._stack.current_index == 1 and panel._stack._animation is None)
    combo = panel.scope
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton)
    view = combo.view()
    qtbot.waitUntil(view.isVisible)
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert not outer._closing
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(view.isVisible)
    qtbot.wait(300)  # Native combo popups briefly suppress the opening click release.
    target = combo.model().index(combo.findData("global"), 0)
    point = view.visualRect(target).center()
    qtbot.mouseMove(view.viewport(), point)
    qtbot.wait(50)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point, delay=30)
    assert not outer._closing, "The combo popup belongs to the memory panel"
    qtbot.waitUntil(lambda: panel.isEnabled() and combo.currentData() == "global")
    panel.items.setCurrentRow(0)
    panel.items.setFocus()
    qtbot.keyClick(panel.items, Qt.Key.Key_Return)
    editor_panel = panel._editor_panel
    assert editor_panel is not None
    qtbot.waitUntil(lambda: editor_panel._motion is None)
    editor_panel.findChild(QPlainTextEdit).setPlainText("本会话版本")
    qtbot.mouseClick(
        editor_panel.findChild(QPushButton, "memoryEditorSave"), Qt.MouseButton.LeftButton,
    )
    qtbot.waitUntil(lambda: not panel.is_editing and panel.isEnabled())
    assert outer.isVisible() and not outer._closing
    assert service.effective_global("c")[0].content == "本会话版本"
    assert service.list() == [original]
    qtbot.mouseClick(host, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert outer._closing
