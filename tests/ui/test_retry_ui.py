"""重试按钮、历史恢复与真实应用接线的回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.chat_view import ChatView
from limbowave.ui.main_window import MainWindow
from tests.unit.test_retry_in_place import _coordinator
from tests.unit.test_run_coordinator import BranchingKernel


@pytest.mark.parametrize("stop", ["aborted", "error"])
@pytest.mark.parametrize("text", ["", "部分输出"])
def test_incomplete_reply_offers_retry_instead_of_fork(
    qtbot: QtBot, stop: str, text: str
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.add_user_message("问题", "user-1")
    view.set_busy(True)
    view.end_assistant(text, "assistant-1", stop_reason=stop, user_message_id="user-1")
    view.set_busy(False)
    user_row = view._rows[0]
    assert user_row._retry_btn.isVisible()
    assert "当前分支" in user_row._retry_btn.toolTip()
    assistants = [row for row in view._rows if row._role == "assistant"]
    assert bool(assistants) == bool(text)
    assert all(row._fork_btn.isHidden() for row in assistants)
    with qtbot.waitSignal(view.retry_requested) as signal:
        qtbot.mouseClick(user_row._retry_btn, Qt.MouseButton.LeftButton)
    assert signal.args == ["user-1"]
    assert user_row._retry_btn.isHidden()


def test_new_request_clears_old_retry_and_late_failure_does_not_restore_it(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("旧请求", "old")
    view.add_error("出错", retry_message_id="old")
    old_user = view._rows[0]
    assert not old_user._retry_btn.isHidden()
    view.add_user_message("新请求", "new")
    view.set_retry_available("old")
    assert old_user._retry_btn.isHidden()
    assert view._rows[-1]._retry_btn.isHidden()


@pytest.mark.parametrize("stop", ["error", "aborted", "interrupted", "send_error"])
@pytest.mark.parametrize("text", ["", "部分输出"])
async def test_history_restores_retry_even_without_assistant_message(
    qtbot: QtBot, stop: str, text: str
) -> None:
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = BranchingKernel()
    coord = _coordinator(kernel, store)
    if stop == "send_error":
        kernel.send_error = RuntimeError("发送失败")
    run_id = await coord.send("原文")
    if stop == "interrupted":
        kernel.say(text)
        await coord.mark_interrupted()
    elif stop != "send_error":
        kernel.say(text, stop=stop)
        kernel.emit("run.settled", {})
    await coord.wait_idle()
    view = ChatView()
    qtbot.addWidget(view)
    with factory() as uow:
        messages = uow.messages.list_for_branch(coord.branch_id)
    view.load_history(app._history_payload(messages, uow_factory=factory))
    user = view._rows[0]
    assert not user._retry_btn.isHidden()
    assert all(row._fork_btn.isHidden() for row in view._rows if row._role == "assistant")
    user_id = store.runs[run_id].user_message_id
    with qtbot.waitSignal(view.retry_requested) as signal:
        user._retry_btn.click()
    assert signal.args == [user_id]

    # 同分支新尝试完成后再加载历史，不应复活旧轮重试按钮。
    kernel.send_error = None
    await coord.retry_user_message(user_id)
    kernel.say("成功")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        messages = uow.messages.list_for_branch(coord.branch_id)
    view.load_history(app._history_payload(messages, uow_factory=factory))
    assert all(row._retry_btn.isHidden() for row in view._rows if row._role == "user")
    assert [row._role for row in view._rows] == ["user", "assistant"]
    assert view._rows[-1].content_text() == "成功"
    assert not view._rows[-1]._fork_btn.isHidden()


@pytest.mark.parametrize("stop", ["error", "aborted", "send_error"])
@pytest.mark.parametrize("text", ["", "部分输出"])
async def test_app_retry_signal_keeps_current_branch(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: str, text: str
) -> None:
    kernel = BranchingKernel()
    monkeypatch.setattr(
        app, "_build_kernel", lambda *_a, **_kw: KernelSetup(kernel, "fake", "fake", "test")
    )
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    window = MainWindow()
    qtbot.addWidget(window)
    controller, _ipc, _warm, _startup, shutdown = app._wire(
        window, AppPaths(tmp_path, tmp_path / "logs"), None
    )
    try:
        if stop == "send_error":
            kernel.send_error = RuntimeError("发送失败")
        await controller.send("问题")
        branch_id = controller.coordinator().branch_id
        if stop == "aborted":
            await controller.abort()
        if stop != "send_error":
            kernel.say(text, stop=stop)
            kernel.emit("run.settled", {})
        await controller.wait_idle()
        user = next(row for row in window.chat._rows if row._role == "user")
        assistant = next(
            (row for row in window.chat._rows if row._role == "assistant"), None
        )
        previous_user_id = user._message_id
        kernel.send_error = None
        sent_count = len(kernel.sent)
        assert not user._retry_btn.isHidden()
        user._retry_btn.click()
        # app 的槽排入 asyncio，运行到下一轮事件循环即可实际重发。
        for _ in range(20):
            if len(kernel.sent) == sent_count + 1:
                break
            await asyncio.sleep(0)
        assert kernel.sent == ["问题"] * (sent_count + 1)
        assert controller.coordinator().branch_id == branch_id
        assert kernel.forked == []
        assert [row._role for row in window.chat._rows] == ["user", "assistant"]
        assert window.chat._rows[0] is user
        assert user._message_id != previous_user_id
        if assistant is not None:
            assert window.chat._rows[-1] is assistant
        assert window.chat._rows[-1]._retry_animation is not None
        kernel.say("成功")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        assert window.chat._rows[-1].content_text() == "成功"
        assert not window.chat._rows[-1]._fork_btn.isHidden()
    finally:
        await shutdown()


@pytest.mark.parametrize("with_content", [False, True])
def test_retry_reuses_rows_and_fades_without_losing_fast_tokens(
    qtbot: QtBot, with_content: bool
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.add_user_message("问题", "user-1")
    view.set_busy(True)
    if with_content:
        view.append_thinking_delta("旧思考")
        view.note_tool_step("read", "tool-1", is_error=False, phase="start")
        view.end_assistant("旧的第一段", "assistant-1")
        view.begin_assistant()
        view.append_assistant_delta("旧的第二段")
    view.end_assistant(
        "旧的第二段" if with_content else "",
        "assistant-2",
        stop_reason="error",
        user_message_id="user-1",
    )
    view.add_retry_notice("网络错误", attempt=2, delay_ms=100, max_attempts=2)
    view.add_error("失败", retry_message_id="user-1")
    view.set_busy(False)
    user = view._rows[0]
    previous = next((row for row in view._rows if row._role == "assistant"), None)

    view.retry_user_message("问题", "user-2", "user-1")
    assert [row._role for row in view._rows] == ["user", "assistant"]
    assert view._rows[0] is user
    assert user._message_id == "user-2"
    assert user._retry_btn.isHidden()
    assert view._transcript.count() == 3  # 两行 + stretch，没有残留提示
    assistant = view._rows[-1]
    if previous is not None:
        assert assistant is previous
    assert len(assistant._segments) == 1
    assert assistant._segments[0].hint._timer.isActive()
    assert assistant._thinking is None
    assert assistant._tool_steps_view is None
    assert not view._active_tool_calls
    assert assistant._actions.isHidden()
    assert assistant._message_id is None
    effect = assistant._assistant_panel.graphicsEffect()
    animation = assistant._retry_animation
    assert effect is not None and animation is not None
    animation.setCurrentTime(120)
    assert effect.opacity() < 0.5

    # 动画尚未结束时整轮就返回，动画的收尾也不能把新内容清掉。
    view.begin_assistant()
    view.append_thinking_delta("新思考")
    view.append_assistant_delta("新回复")
    view.end_assistant("新回复", "assistant-new")
    view.set_busy(False)
    qtbot.waitUntil(lambda: assistant._retry_animation is None, timeout=1500)
    assert assistant._assistant_panel.graphicsEffect() is None
    assert assistant.content_text() == "新回复"
    assert assistant._thinking._content.text() == "新思考"
    assert assistant._message_id == "assistant-new"
    assert not assistant._fork_btn.isHidden()
    assert len(assistant._segments) == 1
    assert not assistant._segments[0].hint._timer.isActive()


@pytest.mark.parametrize("cleanup", ["clear", "history"])
def test_repeated_retry_and_view_cleanup_cancel_animation(qtbot: QtBot, cleanup: str) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("问题", "user-0")
    view.set_busy(True)
    for attempt in range(3):
        view.end_assistant(
            "部分输出", f"assistant-{attempt}",
            stop_reason="aborted", user_message_id=f"user-{attempt}",
        )
        view.set_busy(False)
        assistant = view._rows[-1]
        with qtbot.waitSignal(view.retry_requested) as signal:
            view._rows[0]._retry_btn.click()
        assert signal.args == [f"user-{attempt}"]
        view.retry_user_message("问题", f"user-{attempt + 1}", f"user-{attempt}")
        assert view._rows[-1] is assistant
        assert len(view._rows) == 2
        assert assistant.content_text() == ""
        assert assistant._retry_animation is not None
    segment = assistant._segments[0]
    if cleanup == "clear":
        view.clear_transcript()
    else:
        view.load_history([("user", "另一会话", "", "other-user")])
    assert assistant._retry_animation is None
    assert not segment.hint._timer.isActive()
    qtbot.wait(400)  # 已移除卡片的动画不能再触发延迟回调
    assert len(view._rows) == (0 if cleanup == "clear" else 1)


async def test_retry_history_collapses_chain_but_not_normal_duplicate_sends(qtbot: QtBot) -> None:
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = BranchingKernel()
    coord = _coordinator(kernel, store)
    run_id = await coord.send("相同问题")
    for _ in range(3):
        kernel.say("旧的部分输出", stop="error")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        run_id = await coord.retry_user_message(store.runs[run_id].user_message_id)
    kernel.say("最新尝试仍失败", stop="error")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    view = ChatView()
    qtbot.addWidget(view)

    def reload_history() -> None:
        with factory() as uow:
            messages = uow.messages.list_for_branch(coord.branch_id)
        view.load_history(app._history_payload(messages, uow_factory=factory))

    reload_history()
    assert len(store.runs) == 4  # 审计仍然完整
    assert [row._role for row in view._rows] == ["user", "assistant"]
    assert view._rows[0]._message_id == store.runs[run_id].user_message_id
    assert not view._rows[0]._retry_btn.isHidden()
    assert view._rows[-1].content_text() == "最新尝试仍失败"
    assert view._rows[-1]._fork_btn.isHidden()

    # 同一句话主动再发送一次是新的一轮，不是重试。
    await coord.send("相同问题")
    kernel.say("普通发送成功")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    reload_history()
    assert [row._role for row in view._rows] == ["user", "assistant", "user", "assistant"]
    assert all(row._retry_btn.isHidden() for row in view._rows if row._role == "user")
    assert view._rows[-1].content_text() == "普通发送成功"
