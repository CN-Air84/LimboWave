"""记忆子选项卡、悬浮编辑器与完整审批正文。"""

import asyncio

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton, QWidget

from limbowave.app import _ask_memory_user
from limbowave.domain.memory import MemoryPolicy
from limbowave.ui.animated_stack import AnimatedPageStack, AnimatedTabBar
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.session_memory_panel import MemoryPanel
from tests.unit.test_memory_service import memory_stack as memory_stack


def _show_editor_page(qtbot, service, scope=(None, None)):
    panel = MemoryPanel(service, conversation_id=scope[0], branch_id=scope[1])
    qtbot.addWidget(panel)
    panel.resize(900, 640)
    panel._subtabs.setCurrentIndex(1)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def _editor_parts(qtbot, panel):
    popup = panel._editor_panel
    assert popup is not None and popup.isVisible()
    qtbot.waitUntil(lambda: popup._motion is None)
    editor = popup.findChild(QPlainTextEdit)
    save = popup.findChild(QPushButton, "memoryEditorSave")
    return popup, editor, save


def _double_click(qtbot, panel, row=0):
    pos = panel.items.visualItemRect(panel.items.item(row)).center()
    qtbot.mouseClick(panel.items.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert not panel.is_editing
    qtbot.mouseDClick(panel.items.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseRelease(panel.items.viewport(), Qt.MouseButton.LeftButton, pos=pos)


def test_memory_subtabs_separate_settings_and_list_with_appearance_animation(qtbot, memory_stack):
    service, _ = memory_stack
    panel = MemoryPanel(service)
    qtbot.addWidget(panel)
    panel.resize(900, 640)
    panel.show()
    qtbot.waitExposed(panel)
    tabs, stack = panel._subtabs, panel._stack
    assert isinstance(tabs, AnimatedTabBar)
    assert isinstance(stack, AnimatedPageStack)
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["记忆注入", "记忆编辑"]
    assert tabs.objectName() == "memorySubtabs"
    assert tabs._indicator.objectName() == "memorySubtabIndicator"
    assert stack.count() == 2
    assert stack.current_index == 0
    assert panel.policy.isVisible() and panel.global_interval.isVisible()
    assert not panel.items.isVisible()
    assert panel.findChildren(QPlainTextEdit) == []
    assert tabs.width() == tabs.preferred_width()
    left = tabs.mapTo(panel, QPoint()).x()
    assert abs(left - (panel.width() - left - tabs.width())) <= 2

    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(1).center())
    assert stack._animation is not None
    stack._animation.setCurrentTime(80)
    assert stack._stack.y() == 0
    assert -14 < stack._stack.x() < 0
    stack._animation.setCurrentTime(160)
    assert stack._stack.pos() == QPoint(18, 0)
    qtbot.waitUntil(lambda: stack.current_index == 1 and stack._animation is None)
    assert panel.items.isVisible() and panel.new_button.isVisible()
    assert not panel.policy.isVisible() and not panel.global_interval.isVisible()
    assert panel.new_button.text() == "添加记忆"
    assert not panel.delete_button.isEnabled()
    assert not panel.promote_button.isVisible()
    assert panel.findChildren(QPlainTextEdit) == []


def test_global_injection_settings_and_intervals(qtbot, memory_stack):
    service, _ = memory_stack
    panel = MemoryPanel(service)
    qtbot.addWidget(panel)
    assert panel.global_interval.minimum() == 1
    assert panel.session_interval.maximum() == 30
    panel.global_interval.setValue(1)
    panel.session_interval.setValue(30)
    panel.policy.setCurrentIndex(panel.policy.findData("allow"))
    qtbot.mouseClick(panel.save_settings_button, Qt.MouseButton.LeftButton)
    assert service.settings().global_interval == 1
    assert service.settings().session_interval == 30
    assert service.settings().default_policy == "allow"
    assert panel.status.text() == "已保存"
    assert not panel.is_editing


@pytest.mark.parametrize("scope", [(None, None), ("c", "b")])
def test_add_and_double_click_edit_memory_in_floating_panel(qtbot, memory_stack, scope):
    service, _ = memory_stack
    panel = _show_editor_page(qtbot, service, scope)
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    popup, editor, save = _editor_parts(qtbot, panel)
    assert popup._title.text() == "添加记忆"
    assert editor.toPlainText() == ""
    assert editor.hasFocus()
    editor.setPlainText("中文偏好\n保留换行")
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert not panel.is_editing
    original = service.list(*scope)[0]
    assert original.content == "中文偏好\n保留换行"
    assert panel.items.count() == 1
    assert panel._subtabs.currentIndex() == 1

    _double_click(qtbot, panel)
    popup, editor, save = _editor_parts(qtbot, panel)
    assert popup._title.text() == "编辑记忆"
    assert editor.toPlainText() == original.content
    editor.setPlainText("<b>完整正文，不解析标签</b>\n" + "记忆" * 1500)
    expected = editor.toPlainText()
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    updated = service.list(*scope)
    assert len(updated) == 1 and updated[0].id == original.id
    assert updated[0].content == expected
    assert panel.items.item(0).toolTip() == expected
    assert panel.items.item(0).text() == expected.splitlines()[0]
    assert not panel.is_editing
    if scope[0]:
        assert not service.list() and not service.list("c", "other")

    panel.items.setCurrentRow(0)
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    popup, editor, save = _editor_parts(qtbot, panel)
    assert editor.toPlainText() == ""
    editor.setPlainText("独立的新条目")
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert len(service.list(*scope)) == 2


@pytest.mark.parametrize("dismiss", ["cancel", "escape", "close", "outside"])
def test_dismissing_editor_does_not_save_changes(qtbot, memory_stack, dismiss):
    service, _ = memory_stack
    original = service.save("原始记忆")
    panel = _show_editor_page(qtbot, service)
    _double_click(qtbot, panel)
    popup, editor, _save = _editor_parts(qtbot, panel)
    editor.setPlainText("未保存的更改")
    if dismiss == "cancel":
        qtbot.mouseClick(
            popup.findChild(QPushButton, "memoryEditorCancel"), Qt.MouseButton.LeftButton,
        )
    elif dismiss == "escape":
        qtbot.keyClick(editor, Qt.Key.Key_Escape)
    elif dismiss == "close":
        qtbot.mouseClick(popup.findChild(QPushButton), Qt.MouseButton.LeftButton)
    else:
        qtbot.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert not panel.is_editing
    assert service.list() == [original]
    _double_click(qtbot, panel)
    popup, editor, _save = _editor_parts(qtbot, panel)
    assert editor.toPlainText() == original.content


@pytest.mark.parametrize("text", ["", "   \n", "字" * 4001])
def test_invalid_memory_stays_in_editor_and_can_be_corrected(qtbot, memory_stack, text):
    service, _ = memory_stack
    panel = _show_editor_page(qtbot, service)
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    popup, editor, save = _editor_parts(qtbot, panel)
    editor.setPlainText(text)
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert panel.is_editing and popup.isVisible()
    assert editor.toPlainText() == text
    assert not service.list()
    error = popup.findChild(QLabel, "memoryEditorError")
    assert "1–4000" in error.text()
    assert error.textFormat() == Qt.TextFormat.PlainText
    editor.setPlainText("可保存的正文")
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert not panel.is_editing
    assert service.list()[0].content == "可保存的正文"


def test_reload_preserves_draft_and_original_edit_target(qtbot, memory_stack):
    service, _ = memory_stack
    original = service.save("原始条目")
    other = service.save("另一条目")
    panel = _show_editor_page(qtbot, service)
    _double_click(qtbot, panel)
    popup, editor, save = _editor_parts(qtbot, panel)
    editor.setPlainText("仍然编辑原始条目")
    panel.reload()
    panel.items.setCurrentRow(1)
    assert panel.is_editing
    assert panel._editor_panel is popup
    assert editor.toPlainText() == "仍然编辑原始条目"
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert service.list()[0].id == original.id
    assert service.list()[0].content == "仍然编辑原始条目"
    assert service.list()[1] == other


def test_hidden_memory_page_closes_editor_without_saving(qtbot, memory_stack):
    service, _ = memory_stack
    panel = _show_editor_page(qtbot, service)
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    _popup, editor, _save = _editor_parts(qtbot, panel)
    editor.setPlainText("未保存的新记忆")
    panel.hide()
    assert not panel.is_editing
    assert service.list() == []


def test_session_editor_is_nested_and_closes_with_parent(qtbot, memory_stack):
    service, _ = memory_stack
    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    host.show()
    outer = FloatingPanel(host, "会话记忆", width=620)
    panel = MemoryPanel(service, outer, conversation_id="c", branch_id="b")
    outer.content_layout.addWidget(panel, 1)
    panel._subtabs.setCurrentIndex(1)
    outer.popup()
    qtbot.waitUntil(lambda: outer._motion is None)
    assert not panel.global_interval.isVisible()
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    popup, editor, save = _editor_parts(qtbot, panel)
    assert popup.parentWidget() is outer
    assert outer.rect().contains(popup.geometry())
    qtbot.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton)
    assert not outer._closing and panel.is_editing
    editor.setPlainText("此分支的记忆")
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert not outer._closing and not panel.is_editing
    assert service.list("c", "b")[0].content == "此分支的记忆"
    assert not service.list()
    qtbot.mouseClick(panel.new_button, Qt.MouseButton.LeftButton)
    popup, editor, save = _editor_parts(qtbot, panel)
    editor.setPlainText("父窗关闭时不保存")
    outer.close_panel()
    assert popup._closing and not panel.is_editing
    assert len(service.list("c", "b")) == 1


def test_session_override_and_promotion(qtbot, memory_stack, monkeypatch):
    service, _ = memory_stack
    service.save("分支独有", "c", "b")
    panel = MemoryPanel(service, conversation_id="c", branch_id="b")
    qtbot.addWidget(panel)
    panel.items.setCurrentRow(0)
    confirmations = []

    def confirm(_parent, _title, detail, callback):
        confirmations.append(detail)
        callback(True)

    monkeypatch.setattr("limbowave.ui.session_memory_panel.ask_confirm", confirm)
    panel._promote()
    assert "所有会话" in confirmations[0]
    assert service.list()[0].content == "分支独有"
    assert service.list("c", "other") == []
    panel.policy.setCurrentIndex(panel.policy.findData("allow"))
    qtbot.mouseClick(panel.save_settings_button, Qt.MouseButton.LeftButton)
    assert service.policy("c") is MemoryPolicy.ALLOW
    panel.items.setCurrentRow(0)
    panel._delete()
    assert not service.list("c", "b")
    assert len(service.list()) == 1
    assert not panel.delete_button.isEnabled()
    assert not panel.promote_button.isEnabled()


async def test_memory_approval_is_plaintext_and_close_denies(qtbot):
    host = QWidget()
    host.resize(900, 800)
    qtbot.addWidget(host)
    host.show()
    text = "<b>not markup</b>" + "完整正文" * 800
    task = asyncio.create_task(_ask_memory_user(host, "记忆确认", text))
    await asyncio.sleep(0)
    panel = host.findChild(FloatingPanel)
    editor = panel.findChild(QPlainTextEdit)
    assert editor.isReadOnly() and editor.toPlainText() == text
    panel.close_panel()
    qtbot.wait(250)
    assert await asyncio.wait_for(task, timeout=2) is False


@pytest.mark.parametrize("scope", [(None, None), ("c", "b")])
def test_memory_cards_use_full_first_line_as_title(qtbot, memory_stack, scope):
    service, _ = memory_stack
    titles = ["中文偏好", "<b>普通文本标题</b>", "很长的第一行" * 40, "单行记忆"]
    for title in titles:
        service.save(title + ("\n正文不应混进标题" if title != "单行记忆" else ""), *scope)
    panel = _show_editor_page(qtbot, service, scope)
    assert [panel.items.item(i).text() for i in range(4)] == titles
    for i, memory in enumerate(service.list(*scope)):
        assert panel.items.item(i).data(Qt.ItemDataRole.UserRole) == memory
        assert panel.items.item(i).toolTip() == memory.content


@pytest.mark.parametrize("width", [420, 620, 900])
def test_memory_cards_keep_two_columns_when_resized_and_scrolled(qtbot, memory_stack, width):
    service, _ = memory_stack
    for i in range(25):
        service.save(f"标题 {i}\n" + "正文预览" * 30)
    panel = _show_editor_page(qtbot, service)
    panel.resize(width, 420)
    view = panel.items

    def two_columns():
        rects = [view.visualItemRect(view.item(i)) for i in range(4)]
        return (
            rects[0].top() == rects[1].top()
            and rects[0].right() < rects[1].left()
            and rects[2].top() == rects[3].top() > rects[0].top()
            and rects[2].left() == rects[0].left()
            and rects[0].width() == rects[1].width()
            and rects[1].right() < view.viewport().width()
        )

    qtbot.waitUntil(two_columns)
    assert view.verticalScrollBar().maximum() > 0
    assert view.horizontalScrollBar().maximum() == 0
    assert not view.horizontalScrollBar().isVisible()
    view.scrollToItem(view.item(24))
    qtbot.waitUntil(lambda: view.viewport().rect().contains(view.visualItemRect(view.item(24))))
    _double_click(qtbot, panel, 24)
    _popup, editor, _save = _editor_parts(qtbot, panel)
    assert editor.toPlainText() == service.list()[24].content
    _popup.close_panel()


def test_memory_cards_keyboard_navigation_and_edit(qtbot, memory_stack):
    service, _ = memory_stack
    for i in range(4):
        service.save(f"记忆 {i}")
    panel = _show_editor_page(qtbot, service)
    panel.items.setFocus()
    panel.items.setCurrentRow(0)
    qtbot.keyClick(panel.items, Qt.Key.Key_Right)
    assert panel.items.currentRow() == 1
    assert panel.delete_button.isEnabled()
    qtbot.keyClick(panel.items, Qt.Key.Key_Down)
    assert panel.items.currentRow() == 3
    qtbot.keyClick(panel.items, Qt.Key.Key_Return)
    _popup, editor, _save = _editor_parts(qtbot, panel)
    assert editor.toPlainText() == "记忆 3"


@pytest.mark.parametrize(("palette", "scale"), [("dark", 1.0), ("light", 1.5)])
def test_memory_cards_empty_state_and_themed_layout(qtbot, memory_stack, palette, scale):
    from limbowave.ui import theme

    service, _ = memory_stack
    old_palette, old_scale = theme.current_palette().name, theme.current_font_scale()
    try:
        theme.set_palette(palette)
        theme.set_font_scale(scale)
        panel = _show_editor_page(qtbot, service)
        panel.setStyleSheet(theme.app_stylesheet())
        assert panel.items.empty_state.isVisible()
        for i in range(5):
            service.save(f"卡片 {i}\n" + "🙂正文 \U00020000" * 70)
        panel.reload()
        assert not panel.items.empty_state.isVisible()
        view = panel.items
        qtbot.waitUntil(lambda: view.visualItemRect(view.item(1)).left() > 0)
        first, second, third = [view.visualItemRect(view.item(i)) for i in range(3)]
        assert first.top() == second.top() < third.top()
        assert first.right() < second.left()
        assert second.right() < view.viewport().width()
        assert first.height() >= view.fontMetrics().lineSpacing() * 3 + 48
        assert not panel.grab().isNull()  # 主题、放大字体及非 BMP 字符均能完成绘制。
        for item in service.list():
            service.delete(item.id)
        panel.reload()
        assert view.empty_state.isVisible()
        assert not panel.delete_button.isEnabled()
    finally:
        theme.set_palette(old_palette)
        theme.set_font_scale(old_scale)
