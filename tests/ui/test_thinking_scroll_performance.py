"""Reasoning streaming must not continuously rebuild invisible documents/layouts."""

from unittest.mock import Mock

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget

from limbowave.ui.chat_view import ChatView, _ThinkingBlock
from limbowave.ui.run_state_label import RunStateLabel


def test_collapsed_reasoning_never_materializes_text(qtbot):
    block = _ThinkingBlock()
    qtbot.addWidget(block)
    block.show()
    for _ in range(2000):
        block.append("长思考片段 <tag> & **raw**\n")
    assert block.has_content()
    assert block._content.toPlainText() == ""
    assert not block._flush_timer.isActive()
    block._on_toggle()
    assert block._content.toPlainText() == block.text()
    assert block._content.document().blockCount() == 2001


def test_expanded_reasoning_batches_and_keeps_selection(qtbot):
    block = _ThinkingBlock()
    qtbot.addWidget(block)
    block.resize(600, 400)
    block.show()
    block.append("保留选择\n")
    block._on_toggle()
    cursor = block._content.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(4, QTextCursor.MoveMode.KeepAnchor)
    block._content.setTextCursor(cursor)
    changes = []
    block._content.document().contentsChanged.connect(lambda: changes.append(1))
    for _ in range(100):
        block.append("追加")
    assert not changes
    qtbot.waitUntil(lambda: block._content.toPlainText() == block.text())
    assert len(changes) == 1
    assert block._content.textCursor().selectedText() == "保留选择"
    assert not block._content.document().isUndoAvailable()


def test_pending_reasoning_survives_collapse_hide_and_replacement(qtbot):
    block = _ThinkingBlock()
    qtbot.addWidget(block)
    block.show()
    block.append("开始")
    block._on_toggle()
    block.append("待刷新")
    block._on_toggle()
    assert not block._flush_timer.isActive()
    assert block._content.toPlainText() == "开始"
    block._on_toggle()
    assert block._content.toPlainText() == "开始待刷新"
    block.hide()
    block.append("隐藏期间")
    assert not block._flush_timer.isActive()
    block.show()
    assert block._content.toPlainText() == block.text()
    block.append("旧尾部")
    block.set_text("替换内容")
    qtbot.wait(80)
    assert block.text() == block._content.toPlainText() == "替换内容"


def test_toggle_works_even_when_parent_is_hidden(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    block = _ThinkingBlock(parent)
    block.append("内容")
    block._on_toggle()
    assert not block._content.isHidden()
    block._on_toggle()
    assert block._content.isHidden()


def test_reasoning_deltas_do_not_resync_whole_card(qtbot, monkeypatch):
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_thinking_delta("首个片段")
    row = view._stream_row
    sync = Mock(wraps=row._sync_segment_presentation)
    monkeypatch.setattr(row, "_sync_segment_presentation", sync)
    for _ in range(500):
        view.append_thinking_delta("后续")
    assert sync.call_count == 0
    assert row._thinking.text() == "首个片段" + "后续" * 500


def test_body_stream_inserts_instead_of_resetting_document(qtbot, monkeypatch):
    view = ChatView()
    qtbot.addWidget(view)
    view.begin_assistant()
    view.append_assistant_delta("原文\n")
    body = view._stream_row.label
    reset = Mock(wraps=body.setPlainText)
    monkeypatch.setattr(body, "setPlainText", reset)
    cursor = body.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
    body.setTextCursor(cursor)
    view.append_assistant_delta("追加 <b>不是HTML</b>")
    assert reset.call_count == 0
    qtbot.waitUntil(lambda: body.toPlainText() == "原文\n追加 <b>不是HTML</b>")
    assert body.textCursor().selectedText() == "原文"


def test_offscreen_wave_does_not_request_repaints(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(400, 200)
    host = QWidget()
    host.resize(380, 2000)
    label = RunStateLabel(host)
    label.setGeometry(0, 1700, 360, 40)
    label.setText("思考中")
    scroll.setWidget(host)
    scroll.show()
    qtbot.waitUntil(lambda: label.visibleRegion().isEmpty())
    update = Mock()
    monkeypatch.setattr(label, "update", update)
    label._tick()
    assert update.call_count == 0
    scroll.verticalScrollBar().setValue(1700)
    qtbot.waitUntil(lambda: not label.visibleRegion().isEmpty())
    label._tick()
    assert update.call_count > 0


def test_error_status_is_static(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    row = view._stream_row
    row.set_run_error("网络错误")
    assert not row._run_state._timer.isActive()
    assert row._run_state.text() == "⚠ 网络错误"


def test_wave_respects_reduced_motion(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: False))
    label = RunStateLabel()
    qtbot.addWidget(label)
    label.setText("思考中")
    label.show()
    assert not label._timer.isActive()
    assert label.property("themeGlowDisabled")


def test_reasoning_height_wraps_and_outer_scroll_follows(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 640)
    view.show()
    view.set_busy(True)
    view.append_thinking_delta("思考中的长行文字 " * 300 + "\n")
    block = view._stream_row._thinking
    block._on_toggle()
    bar = view._scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 100)
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())
    before = bar.maximum()
    for _ in range(50):
        view.append_thinking_delta("接下来的思考\n")
    qtbot.waitUntil(lambda: bar.maximum() > before)
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())
    qtbot.waitUntil(lambda: block._content.verticalScrollBar().maximum() == 0)
    height = block._content.height()
    view.resize(640, 640)
    qtbot.waitUntil(lambda: block._content.height() > height)
    qtbot.waitUntil(lambda: block._content.verticalScrollBar().maximum() == 0)


def test_reasoning_stream_does_not_steal_scrolled_up_position(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 640)
    view.show()
    view.load_history([("assistant", "历史消息\n" * 10, "", str(i)) for i in range(10)])
    view.set_busy(True)
    view.append_thinking_delta("思考起点\n")
    block = view._stream_row._thinking
    block._on_toggle()
    bar = view._scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 500)
    qtbot.wait(250)  # let initial docking/geometry settle
    bar.setValue(120)
    assert not view._stick_to_bottom
    for _ in range(100):
        view.append_thinking_delta("继续推理\n")
    qtbot.waitUntil(lambda: block._content.toPlainText() == block.text())
    qtbot.wait(80)
    assert bar.value() == 120
    assert not view._stick_to_bottom
    block._on_toggle()
    for _ in range(100):
        view.append_thinking_delta("折叠后\n")
    qtbot.wait(80)
    assert bar.value() == 120
    assert not view._stick_to_bottom


def test_wave_reuses_geometry_until_resize(qtbot):
    label = RunStateLabel()
    qtbot.addWidget(label)
    label.resize(760, 36)
    label.show()
    label._timer.stop()
    label.grab()
    paths = tuple(label._waves)
    label._phase = 1.5
    label.grab()
    assert all(old is new for old, new in zip(paths, label._waves, strict=True))
    label.resize(400, 36)
    label.grab()
    assert label._waves[0] is not paths[0]


def test_history_reasoning_is_lazy_and_empty_deltas_are_ignored(qtbot):
    block = _ThinkingBlock()
    qtbot.addWidget(block)
    block.set_text("历史思考\n" * 1000)
    block.append("")
    assert len(block._chunks) == 1
    assert block._content.document().isEmpty()
    block._on_toggle()
    assert block._content.toPlainText() == block.text()
    block.set_text("")
    assert not block.has_content()
    assert block._content.document().isEmpty()
    block.append(" \n")
    assert not block.has_content()
    block.append("新的推理")
    assert block.has_content()


def test_static_error_can_return_to_animated_run(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    row = view._stream_row
    row.set_run_error("网络错误")
    assert not row._run_state._timer.isActive()
    row.set_run_state("正在重试")
    assert row._run_state._timer.isActive()
