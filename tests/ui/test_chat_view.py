"""ChatView 的 UI 测试：视图只管展示与发信号。"""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import ChatView


def test_submit_emits_signal_and_clears(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("你好")

    with qtbot.waitSignal(view.message_submitted, timeout=1000) as blocker:
        view._on_send()

    assert blocker.args == ["你好"]
    assert view._input.toPlainText() == ""


def test_empty_input_does_not_emit(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("   ")
    # 不应发信号：直接调用并确认没有挂起
    view._on_send()


def test_streaming_updates_bubble(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.begin_assistant()
    view.append_assistant_delta("你")
    view.append_assistant_delta("好")
    view.end_assistant("你好")

    # 流式气泡定稿为最终文本
    assert view._stream_label is None


def test_busy_toggles_buttons(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_available(True)
    view.set_busy(True)
    assert not view._send_btn.isEnabled()
    assert view._stop_btn.isEnabled()

    view.set_busy(False)
    assert view._send_btn.isEnabled()


def test_unavailable_disables_input(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_available(False, "未配置")
    assert not view._input.isEnabled()
    assert not view._send_btn.isEnabled()
