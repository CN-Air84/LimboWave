"""压缩期间的思考过程：不进消息区气泡，改由单独的悬浮窗展示。

锁住的行为约定：
- 压缩中，助手流（开始 / 思考 / 正文 / 定稿）一律不建气泡；
- 思考流进「压缩 · 思考过程」悬浮窗，首个思考 delta 到来才弹出，没有思考就不弹；
- 摘要正文不进悬浮窗（留给压缩预览），悬浮窗只切到「正在生成摘要」；
- 开始压缩时正有回复在流式，那条回复照旧进它自己的气泡；
- 用户关掉悬浮窗后这一轮不再弹出，下一轮压缩重新弹；
- 压缩结束后，正常回复的思考照旧折叠在气泡里。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from limbowave.ui.chat_view import ChatView
from limbowave.ui.compression_widgets import CompressionThinkingPanel


def _view(qtbot: QtBot) -> ChatView:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    return view


def _stream_compression_output(view: ChatView, thinking: list[str], summary: str) -> None:
    """按 app._on_event 转发控制器事件的次序，喂一轮压缩模型的输出。"""
    view.begin_assistant()
    for delta in thinking:
        view.append_thinking_delta(delta)
    view.append_assistant_delta(summary)
    view.end_assistant(summary)


# ---------- ChatView：压缩期间的助手流不建气泡 ----------


def test_compression_thinking_goes_to_floating_panel_not_bubble(qtbot: QtBot) -> None:
    view = _view(qtbot)
    view.add_user_message("之前的问题", "m1")
    rows_before = len(view._rows)
    view.begin_compression(40.0)

    view.begin_assistant()
    view.append_thinking_delta("先通读")
    view.append_thinking_delta("上下文")

    panel = view.compression_thinking_panel
    assert isinstance(panel, CompressionThinkingPanel)
    assert panel.isVisible()
    assert panel.parentWidget() is view.window()
    assert panel.thinking_text == "先通读上下文"
    assert panel.status_text == CompressionThinkingPanel.THINKING

    view.append_assistant_delta("摘要正文")
    view.end_assistant("摘要正文")

    assert len(view._rows) == rows_before  # 没有新气泡
    assert view._stream_row is None
    assert panel.thinking_text == "先通读上下文"  # 摘要正文不进思考悬浮窗
    assert panel.status_text == CompressionThinkingPanel.SUMMARIZING


def test_compression_without_thinking_pops_nothing(qtbot: QtBot) -> None:
    view = _view(qtbot)
    view.begin_compression(40.0)

    _stream_compression_output(view, [], "摘要")

    assert view.compression_thinking_panel is None
    assert not view.findChildren(CompressionThinkingPanel)
    assert view._rows == []


def test_panel_outlives_compression_and_replies_use_bubbles_again(qtbot: QtBot) -> None:
    view = _view(qtbot)
    view.begin_compression(40.0)
    _stream_compression_output(view, ["想一想"], "摘要")
    panel = view.compression_thinking_panel
    assert panel is not None

    view.finish_compression(12.0)
    assert panel.status_text == CompressionThinkingPanel.FINISHED
    qtbot.waitUntil(lambda: not view.compressing, timeout=3000)
    assert panel.isVisible()  # 压缩结束后留着给用户看完，由用户自己关

    # 压缩结束后的正常回复：照旧进气泡，思考折叠在气泡里
    view.add_user_message("新问题", "m1")
    view.set_busy(True)
    view.begin_assistant()
    view.append_thinking_delta("正常的思考")
    view.end_assistant("正常的回答", "m2")

    row = view._rows[-1]
    assert "正常的回答" in row.content_text()
    assert row._thinking is not None
    assert row._thinking._content.text() == "正常的思考"
    assert panel.thinking_text == "想一想"


def test_compression_does_not_hijack_a_streaming_reply(qtbot: QtBot) -> None:
    """开始压缩时正有回复在流式：那条回复照旧进它自己的气泡。"""
    view = _view(qtbot)
    view.add_user_message("问题", "m1")
    view.set_busy(True)
    view.begin_assistant()
    view.begin_compression(40.0)

    view.append_thinking_delta("回复的思考")
    view.append_assistant_delta("回复")
    view.end_assistant("回复", "m2")

    assert view.compression_thinking_panel is None
    row = view._rows[-1]
    assert "回复" in row.content_text()
    assert row._thinking is not None
    assert row._thinking._content.text() == "回复的思考"


def test_dismissed_panel_stays_closed_until_next_round(qtbot: QtBot) -> None:
    view = _view(qtbot)
    view.begin_compression(40.0)
    view.begin_assistant()
    view.append_thinking_delta("第一段")
    first = view.compression_thinking_panel
    assert first is not None

    first.close_panel()  # 用户点了关闭 / Esc / 面板外部
    assert view.compression_thinking_panel is None
    view.append_thinking_delta("第二段")
    assert view.compression_thinking_panel is None  # 这一轮不再弹
    view.end_assistant("摘要")
    assert view._rows == []

    view.finish_compression(None)
    qtbot.waitUntil(lambda: not view.compressing, timeout=3000)

    view.begin_compression(40.0)  # 重试：新一轮会重新弹出
    view.begin_assistant()
    view.append_thinking_delta("重试时的思考")
    second = view.compression_thinking_panel
    assert second is not None
    assert second is not first
    assert second.thinking_text == "重试时的思考"


def test_new_round_closes_previous_panel(qtbot: QtBot) -> None:
    view = _view(qtbot)
    view.begin_compression(40.0)
    view.append_thinking_delta("上一轮")
    old = view.compression_thinking_panel
    assert old is not None

    view.finish_compression(10.0)
    view.begin_compression(30.0)  # 上一轮收尾动画未完就开始新一轮

    assert view.compression_thinking_panel is None
    qtbot.waitUntil(lambda: not isValid(old) or not old.isVisible(), timeout=2000)
    view.append_thinking_delta("这一轮")
    current = view.compression_thinking_panel
    assert current is not None
    assert current is not old
    assert current.thinking_text == "这一轮"


# ---------- CompressionThinkingPanel 本身 ----------


@pytest.fixture
def host(qtbot: QtBot) -> QWidget:
    """面板的父窗口。必须由测试一直持有：qtbot 只存弱引用，父窗口被回收会连带删掉面板。"""
    widget = QWidget()
    widget.resize(900, 700)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def _panel(host: QWidget) -> CompressionThinkingPanel:
    panel = CompressionThinkingPanel(host)
    panel.popup()
    return panel


def test_panel_follows_bottom_unless_user_scrolls_up(qtbot: QtBot, host: QWidget) -> None:
    panel = _panel(host)
    bar = panel._text.verticalScrollBar()
    for _ in range(60):
        panel.append("很长的一行思考\n")
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum())

    bar.setValue(0)  # 用户往上翻：后续思考不把视图拉回底部
    old_max = bar.maximum()
    for _ in range(20):
        panel.append("更多思考\n")
    qtbot.waitUntil(lambda: bar.maximum() > old_max)
    assert bar.value() == 0

    bar.setValue(bar.maximum())  # 滚回底部：恢复跟随
    old_max = bar.maximum()
    for _ in range(20):
        panel.append("继续思考\n")
    qtbot.waitUntil(lambda: bar.maximum() > old_max)
    assert bar.value() == bar.maximum()


def test_panel_append_keeps_user_selection(host: QWidget) -> None:
    panel = _panel(host)
    panel.append("第一段")
    cursor = panel._text.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
    panel._text.setTextCursor(cursor)

    panel.append("第二段")

    assert panel.thinking_text == "第一段第二段"
    assert panel._text.textCursor().selectedText() == "第一"
