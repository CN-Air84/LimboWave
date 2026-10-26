"""Zen is reversible presentation, never an execution or permission setting."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextCursor
from pytestqt.qtbot import QtBot

from limbowave.application.history_payload import HistoryEntry
from limbowave.domain.tool_step import ToolStep
from limbowave.ui.chat_view import HISTORY_PAGE, ChatView
from limbowave.ui.main_window import MainWindow


@pytest.fixture
def view(qtbot: QtBot) -> ChatView:
    chat = ChatView()
    qtbot.addWidget(chat)
    chat.resize(1100, 760)
    chat.show()
    return chat


def history() -> list[HistoryEntry]:
    return [
        HistoryEntry("user", "今天有点累。", "", "u1"),
        HistoryEntry("assistant", "慢慢说，\n\n我在听。", "internal reasoning", "a1",
                     tool_steps=(ToolStep(tool_call_id="t1", name="read_file"),)),
    ]


def test_toggle_keeps_draft_and_recovers_after_loading(view, qtbot):
    view._input.setPlainText("还没说完的心事")
    draft = view.snapshot_draft()
    with qtbot.waitSignal(view.zen_mode_changed) as changed:
        qtbot.mouseClick(view._zen_btn, Qt.MouseButton.LeftButton)
    assert changed.args == [True]
    assert view.zen_mode
    assert view._zen_btn.isChecked()
    assert view._zen_btn.accessibleName() == "退出倾诉模式"
    assert view.snapshot_draft() == draft
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    for widget in (view._logical_model, view._advanced_btn, view._permission_btn, view._usage_bar):
        assert widget.isHidden()
    assert not view._attach_btn.isHidden()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    qtbot.waitUntil(overlay.isVisible)
    assert view.childAt(view._zen_btn.mapTo(view, view._zen_btn.rect().center())) is overlay
    assert view.snapshot_draft() == draft
    view.set_history_loading(False)
    qtbot.waitUntil(overlay.isHidden)
    qtbot.mouseClick(view._zen_btn, Qt.MouseButton.LeftButton)
    assert not view.zen_mode
    assert view.snapshot_draft() == draft


@pytest.mark.parametrize("enabled_before_load", [False, True])
def test_history_updates_in_place_and_restores_details(view, enabled_before_load):
    view.set_zen_mode(enabled_before_load)
    view.load_history(history())
    rows = list(view._rows)
    assistant = rows[-1]
    part = assistant._segments[0]
    view.set_zen_mode(True)
    assert part.thinking.isHidden()
    assert part.tool_steps_view.isHidden()
    assert assistant._fork_btn.isHidden()
    assert assistant._regenerate_btn.isHidden()
    assert not assistant._copy_btn.isHidden()
    view._rows[0].eventFilter(view._rows[0]._content, QEvent(QEvent.Type.Enter))
    assert view._rows[0]._edit_btn.isHidden()
    view.set_zen_mode(False)
    assert view._rows == rows
    assert not part.thinking.isHidden()
    assert not part.tool_steps_view.isHidden()
    assert not assistant._fork_btn.isHidden()
    assert assistant.content_text() == "慢慢说，\n我在听。"


def test_tool_visibility_preference_survives_zen_and_updates(view):
    view.load_history(history())
    view.set_tool_steps_visible(False)
    view.set_zen_mode(True)
    view.set_zen_mode(False)
    assert view._tool_steps_hidden
    assert view._rows[-1]._tool_steps_view.isHidden()
    view.set_zen_mode(True)
    view.set_tool_steps_visible(True)
    assert view._rows[-1]._tool_steps_view.isHidden()
    view.set_zen_mode(False)
    assert not view._rows[-1]._tool_steps_view.isHidden()


def test_stream_tools_and_errors_remain_safe(view, qtbot):
    view.set_zen_mode(True)
    view.add_user_message("你好", "u1")
    view.set_busy(True)
    view.begin_assistant()
    row = view._stream_row
    view.append_thinking_delta("private reasoning")
    view.note_tool_step("read_file", "t1", is_error=False, phase="start")
    assert row._thinking.isHidden()
    assert row._tool_steps_view.isHidden()
    assert row._run_state.text() == "正在回应…"
    assert not row._run_state.isHidden()
    assert not view._stop_btn.isHidden()
    with qtbot.waitSignal(view.stop_requested):
        view._stop_btn.click()
    view.append_assistant_delta("我在")
    view.set_zen_mode(False)
    assert row._run_state.text() == "● 正在生成内容…"
    view.set_zen_mode(True)
    view.append_assistant_delta("这里。")
    view.end_assistant("我在这里。", message_id="a1")
    view.add_error("连接中断，请重试。", retry_message_id="u1")
    assert row.content_text() == "我在这里。"
    assert "连接中断" in row._run_state.text()
    assert not row._run_state.isHidden()
    view.set_busy(False)
    assert not row._run_state.isHidden()


def test_technical_only_history_does_not_leave_blank_rows(view):
    view.load_history([HistoryEntry("assistant", "", "reasoning only", "a1")])
    row = view._rows[0]
    assert not row.isHidden()
    view.set_zen_mode(True)
    assert row.isHidden()
    view.set_zen_mode(False)
    assert not row.isHidden()


def test_more_history_inherits_mode(view):
    messages = [HistoryEntry("user", f"消息 {n}", "", f"u{n}") for n in range(HISTORY_PAGE + 4)]
    view.set_zen_mode(True)
    view.load_history(messages)
    view._load_earlier()
    assert len(view._rows) == len(messages)
    assert all(row._zen_mode for row in view._rows)
    view.clear_transcript()
    view.load_history(history())
    assert view.zen_mode
    assert view._rows[-1]._thinking.isHidden()


def test_typography_preserves_content_selection_and_restores(view):
    view.load_history(history())
    body = view._rows[-1]._segments[0].content
    doc = body.document()
    normal_height = doc.firstBlock().blockFormat().lineHeight()
    normal_font = doc.defaultFont()
    cursor = body.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    body.setTextCursor(cursor)
    selected = cursor.selectedText()
    normal_spacing = view._transcript.spacing()
    view.set_zen_mode(True)
    assert doc.firstBlock().blockFormat().lineHeight() >= 150
    assert view._transcript.spacing() > normal_spacing
    assert body.textCursor().selectedText() == selected
    view.set_zen_mode(False)
    assert doc.firstBlock().blockFormat().lineHeight() == normal_height
    assert doc.defaultFont() == normal_font
    assert body.textCursor().selectedText() == selected


def test_window_restores_navigation_and_advanced_panel(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message("你好")
    window.toolbar.set_collapsed(False)
    qtbot.waitUntil(lambda: window.chat.advanced_progress == 1.0)
    sidebar_width = window.sidebar.width()
    window.chat.set_zen_mode(True)
    qtbot.waitUntil(lambda: not window._sidebar_reveal.motion.running
                   and not window.chat._zen_motion.running)
    assert window.sidebar.isHidden()
    assert not window.toolbar.collapsed
    assert not window.toolbar.isVisible()
    window.chat._place_advanced_panel()
    assert not window.toolbar.isVisible()
    window.chat.set_zen_mode(False)
    qtbot.waitUntil(lambda: window.toolbar.isVisible()
                   and not window._sidebar_reveal.motion.running)
    assert window.sidebar.isVisible()
    assert abs(window.sidebar.width() - sidebar_width) <= 1


def test_preview_and_keyboard_share_window_mode(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.chat.set_zen_mode(True)
    preview = window.prepare_history_preview()
    window.show_history_preview()
    assert preview.zen_mode
    window.activateWindow()
    preview._input.setFocus()
    qtbot.waitUntil(lambda: preview._input.hasFocus())
    qtbot.keyClick(preview._input, Qt.Key.Key_F,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: not preview.zen_mode)
    assert not window.chat.zen_mode
    preview.set_zen_mode(True)
    qtbot.keyClick(preview._input, Qt.Key.Key_Escape)
    assert not window.chat.zen_mode
    assert not preview.zen_mode
    window.clear_history_preview()
    assert not window.chat.zen_mode


def test_compression_reasoning_does_not_open_popup(view):
    view.set_zen_mode(True)
    view.begin_compression(50)
    view.append_thinking_delta("compression reasoning")
    panel = view.compression_thinking_panel
    assert panel is not None
    assert not panel.isVisible()
    view.set_zen_mode(False)
    assert panel.isVisible()
    view._reset_thinking_panel()




def test_attachments_permissions_and_input_behaviour_are_unchanged(view, qtbot):
    from limbowave.domain.permissions import PermissionPreset

    view._attachment_bar.add_attachment("file1", "日记.txt", "文字附件")
    view._input.setPlainText("想说的话")
    before = view.snapshot_draft()
    preset = view._permission_preset
    mode = view.execution_mode
    changes = []
    view.zen_mode_changed.connect(changes.append)
    view.set_zen_mode(True)
    view.set_zen_mode(True)
    assert changes == [True]
    assert view.snapshot_draft() == before
    assert view._permission_preset is preset
    assert preset is PermissionPreset.READ_ONLY
    assert view.execution_mode == mode
    with qtbot.waitSignal(view.message_submitted) as sent:
        qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert sent.args == ["想说的话"]


def test_zen_escape_does_not_dismiss_permission_confirmation(qtbot):
    from limbowave.ui.floating import ask_confirm

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.set_zen_mode(True)
    replies = []
    panel = ask_confirm(window, "需要授权", "允许本次操作？", replies.append)
    assert panel.isVisible()
    qtbot.keyClick(panel, Qt.Key.Key_Escape)
    assert replies == [False]
    assert window.chat.zen_mode


def test_standalone_escape_and_collapsed_navigation_restore(view, qtbot):
    view.set_zen_mode(True)
    qtbot.keyClick(view._input, Qt.Key.Key_Escape)
    assert not view.zen_mode
    window = MainWindow()
    qtbot.addWidget(window)
    window.sidebar.hide()
    window.toolbar.set_collapsed(True)
    window.chat.set_zen_mode(True)
    window.chat.set_zen_mode(False)
    assert window.sidebar.isHidden()
    assert window.toolbar.collapsed


def test_streaming_typography_and_final_markdown_survive_toggles(view, qtbot):
    view.set_zen_mode(True)
    view.set_busy(True)
    view.begin_assistant()
    view.append_assistant_delta("第一段\n\n第二段")
    body = view._stream_row._segments[0].content
    qtbot.waitUntil(lambda: "第二段" in body.toPlainText())
    assert body.document().lastBlock().blockFormat().lineHeight() == 160
    view.set_zen_mode(False)
    assert body.document().lastBlock().blockFormat().lineHeight() == 0
    view.set_zen_mode(True)
    view.end_assistant("**第一段**\n\n[第二段](https://example.org)", "a1")
    assert body.document().lastBlock().blockFormat().lineHeight() == 160
    view.set_busy(False)
    view.set_zen_mode(False)
    assert body.document().lastBlock().blockFormat().lineHeight() == 0
    assert "https://example.org" in body.toHtml()
    assert "font-weight:700" in body.toHtml()


def test_compression_markers_hide_and_restore_on_history_reload(view):
    from datetime import UTC, datetime

    from limbowave.domain.compaction import (
        CompressionContext,
        CompressionStatus,
        CompressionVersion,
    )
    from limbowave.ui.compression_widgets import CompressionDivider

    marker = CompressionContext(CompressionVersion(
        id="cmp1", conversation_id="c1", branch_id="b1", created_at=datetime.now(UTC),
        status=CompressionStatus.ACCEPTED, input_message_ids=("a1",),
        tokens_before=1000, tokens_after=200,
    ), is_active=True)
    view.set_zen_mode(True)
    view.load_history([HistoryEntry("assistant", "还在这里。", "", "a1", compressions=(marker,))])
    divider = view.findChildren(CompressionDivider)[0]
    assert divider.isHidden()
    view.set_zen_mode(False)
    assert not divider.isHidden()


def test_reading_position_is_preserved_and_new_output_does_not_pull_to_bottom(view, qtbot):
    from PySide6.QtCore import QPoint

    view.load_history([HistoryEntry("user", f"第 {i} 条消息 " * 10, "", f"u{i}")
                       for i in range(35)])
    bar = view._scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 1000)
    bar.setValue(bar.maximum() // 2)
    viewport = view._scroll.viewport()
    anchor = next(row for row in view._rows
                  if row.mapTo(viewport, QPoint()).y() + row.height() > 0)
    y = anchor.mapTo(viewport, QPoint()).y()
    view.set_zen_mode(True)
    qtbot.waitUntil(lambda: view._history_scroll_anchor is None)
    assert abs(anchor.mapTo(viewport, QPoint()).y() - y) <= 2
    assert view._user_scrolled_up
    view.begin_assistant()
    view.append_assistant_delta("新的回复\n" * 20)
    qtbot.wait(100)
    assert bar.value() < bar.maximum() - 100
    view.set_zen_mode(False)
    qtbot.waitUntil(lambda: view._history_scroll_anchor is None)
    assert abs(anchor.mapTo(viewport, QPoint()).y() - y) <= 2


def test_suspended_compression_panel_keeps_content_and_does_not_capture_clicks(view, qtbot):
    view.begin_compression(50)
    view.append_thinking_delta("first part")
    panel = view.compression_thinking_panel
    assert panel.isVisible()
    view.set_zen_mode(True)
    assert not panel.isVisible()
    assert not panel._outside_filter_installed
    qtbot.mouseClick(view._scroll.viewport(), Qt.MouseButton.LeftButton)
    view.append_thinking_delta(" and more")
    assert view.compression_thinking_panel is panel
    view.set_zen_mode(False)
    assert panel.isVisible()
    assert "first part and more" in panel._text.toPlainText()
    view._reset_thinking_panel()


@pytest.mark.parametrize("width", [360, 640, 1280])
def test_zen_column_is_bounded_and_exit_button_accessible(view, qtbot, width):
    view.resize(width, 740)
    view.set_zen_mode(True)
    view.load_history(history())
    qtbot.waitUntil(lambda: view._composer.width() <= 720)
    assert view._column_width <= 720
    assert view._zen_btn.isVisible()
    assert view.rect().contains(view._zen_btn.geometry().translated(view._zen_btn.parent().pos()))


def test_shortcut_can_enter_zen_from_sidebar_and_ignores_full_page(qtbot):
    from PySide6.QtWidgets import QWidget

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.sidebar.setFocus()
    qtbot.waitUntil(lambda: window.isActiveWindow())
    qtbot.keyClick(window.sidebar, Qt.Key.Key_F,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert window.chat.zen_mode
    page = QWidget()
    window.show_full_page(page)
    qtbot.waitUntil(lambda: not window.transition_active)
    page.setFocus()
    qtbot.keyClick(page, Qt.Key.Key_F,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert window.chat.zen_mode


def test_entering_zen_closes_open_technical_popup(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.toolbar._open_endpoint_panel()
    assert window.toolbar._endpoint_panel is not None
    window.chat.set_zen_mode(True)
    assert window.toolbar._endpoint_panel is None
    assert window.chat.zen_mode


@pytest.mark.parametrize("zen", [False, True])
@pytest.mark.parametrize("width", [360, 640, 1100])
def test_zen_entry_is_a_small_gutter_overlay_without_a_header(view, qtbot, zen, width):
    from PySide6.QtCore import QPoint

    view.resize(width, 740)
    view.set_zen_mode(zen)
    view.load_history(history() * 15)
    qtbot.waitUntil(lambda: view._scroll.verticalScrollBar().maximum() > 0)
    button = view._zen_btn
    viewport = view._scroll.viewport()
    assert view._scroll.y() == 0
    capsule = view._mode_capsule
    assert button.parentWidget() is capsule
    assert capsule.parentWidget() is viewport
    assert capsule.width() == capsule.collapsed_width
    assert button.size().width() == button.size().height() == 28
    assert button.text() == ""
    assert button.accessibleName() == ("退出倾诉模式" if zen else "倾诉模式")
    assert "纯净模式" in button.toolTip()
    assert viewport.rect().contains(capsule.geometry())
    assert not any(label.text() == "只专注于此刻的对话"
                   for label in view.findChildren(type(view._search_label)))
    # The compact overlay can overlap the narrow gutter; it never reserves a row.
    assert capsule.geometry().top() == 8
    position = button.pos()
    view._scroll.verticalScrollBar().setValue(0)
    assert button.pos() == position
    # Only the icon intercepts clicks, not a full-width child masking the message stream.
    assert view.childAt(button.mapTo(view, button.rect().center())) is button
    hit = view.childAt(QPoint(view.width() // 2, 5))
    assert hit is viewport or viewport.isAncestorOf(hit)


def test_zen_entry_has_no_background_band_over_wallpaper(view, qtbot, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage

    from limbowave.ui.backdrop import BackdropEngine

    wallpaper = QImage(300, 500, QImage.Format.Format_RGB32)
    wallpaper.fill(QColor("#315b91"))
    filename = tmp_path / "wallpaper.png"
    assert wallpaper.save(str(filename))
    engine = BackdropEngine(view)
    try:
        assert engine.prepare(str(filename), 0)
        view.set_backdrop_engine(engine)
        view.load_history([HistoryEntry("user", "消息 " * 10, "", f"u{i}") for i in range(30)])
        qtbot.waitUntil(lambda: view._scroll.verticalScrollBar().maximum() > 0)
        assert view._scroll.mapTo(view, QPoint()).y() == 0
        view.set_history_loading(True)
        overlay = view._history_loading_overlay
        qtbot.waitUntil(lambda: overlay._opacity == 1.0)
        assert view.childAt(view._zen_btn.mapTo(view, view._zen_btn.rect().center())) is overlay
        view.resize(850, 700)
        qtbot.waitUntil(lambda: overlay.geometry() == view.rect())
        assert view.childAt(view._zen_btn.mapTo(view, view._zen_btn.rect().center())) is overlay
    finally:
        view.set_history_loading(False)
        view.set_backdrop_engine(None)
        engine.close()
