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
    assert len(assistants) == 1
    assert all(row._fork_btn.isHidden() for row in assistants)
    assert all(row._regenerate_btn.isHidden() for row in assistants)
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
    assert assistant._thinking.text() == "新思考"
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


@pytest.mark.parametrize("old_text", ["", "旧尝试的部分回复"])
def test_automatic_retry_reuses_card_and_replaces_attempt_state(
    qtbot: QtBot, old_text: str
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("之前的问题", "user-old")
    view.end_assistant("之前的回复", "assistant-old")
    earlier_rows = list(view._rows)
    view.add_user_message("当前问题", "user-1")
    view.set_busy(True)
    user, assistant = view._rows[-2:]
    state = assistant._run_state

    for attempt in (2, 3):
        view.begin_assistant()
        view.append_thinking_delta("旧尝试的思考")
        view.end_assistant(
            old_text, "assistant-1", stop_reason="error", user_message_id="user-1"
        )
        view.add_error("连接失败", retry_message_id="user-1")
        old_segment = assistant._segments[-1]
        view.add_retry_notice("连接失败", attempt=attempt, delay_ms=100, max_attempts=3)

        assert view._rows == [*earlier_rows, user, assistant]
        assert view._transcript.count() == len(view._rows) + 1
        assert view._stream_row is assistant
        assert view._run_tail is assistant
        assert view._run_rows == [assistant]
        assert assistant._run_state is state
        assert f"第 {attempt}/3 次尝试" in state.text()
        assert not state.isHidden()
        assert user._retry_btn.isHidden()
        assert assistant.content_text() == ""
        assert assistant._thinking is None
        assert assistant._message_id is None
        assert len(assistant._segments) == 1
        assert assistant._segments[0] is not old_segment
        assert assistant._segments[0].hint._timer.isActive()
        assert not old_segment.hint._timer.isActive()

    view.begin_assistant()
    view.append_thinking_delta("新思考")
    view.append_assistant_delta("成功回复")
    view.end_assistant("成功回复", "assistant-new")
    view.set_busy(False)
    assert view._rows == [*earlier_rows, user, assistant]
    assert assistant.content_text() == "成功回复"
    assert assistant._thinking.text() == "新思考"
    assert len(assistant._segments) == 1
    assert state.isHidden()
    assert not assistant._fork_btn.isHidden()


def test_replayed_manual_retry_does_not_add_or_reset_bubbles(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("问题", "user-1")
    view.set_busy(True)
    view.end_assistant(
        "部分输出", "assistant-1", stop_reason="error", user_message_id="user-1"
    )
    view.set_busy(False)
    view.retry_user_message("问题", "user-2", "user-1")
    user, assistant = view._rows
    view.begin_assistant()
    view.append_assistant_delta("新的输出")

    view.retry_user_message("问题", "user-2", "user-1")

    assert view._rows == [user, assistant]
    assert view._stream_row is assistant
    assert assistant.content_text() == "新的输出"
    view.set_busy(False)


@pytest.mark.parametrize("final_text", ["", "最终回复"])
@pytest.mark.parametrize("final_stop", ["stop", "error"])
async def test_app_automatic_retry_keeps_one_reply_and_only_final_error(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    final_stop: str, final_text: str,
) -> None:
    from tests.unit.test_run_coordinator import _retry_context

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
    controller.coordinator()._context = lambda: _retry_context(base_delay_ms=0)
    try:
        await controller.send("问题")
        user, assistant = window.chat._rows
        for _ in range(2):
            kernel.emit("message.start", {"message": {"role": "assistant"}})
            kernel.emit("message.update", {"assistantMessageEvent": {
                "type": "thinking_delta", "delta": "旧思考",
            }})
            kernel.emit("message.end", {"message": {
                "role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET",
            }})
            assert window.chat._rows == [user, assistant]  # Error event itself adds nothing.
            kernel.emit("run.settled", {})
            await controller.wait_idle()
            assert window.chat._rows == [user, assistant], [
                (row._role, row.content_text()) for row in window.chat._rows
            ]
            assert window.chat._transcript.count() == 3
            assert window.chat._busy
            assert len(assistant._segments) == 1
            assert assistant._thinking is None
            assert user._retry_btn.isHidden()

        assert kernel.sent == ["问题"] * 3
        kernel.say(final_text, stop=final_stop)
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        if final_text or final_stop in {"error", "aborted"}:
            assert window.chat._rows == [user, assistant]
            assert assistant.content_text() == final_text
            assert len(assistant._segments) == 1
        else:
            assert all(row._role != "assistant" for row in window.chat._rows)
        assert not window.chat._busy
        assert all(row._role != "error" for row in window.chat._rows)
        if final_stop == "error":
            assert assistant._run_error
            assert not assistant._run_state.isHidden()
            assert "⚠" in assistant._run_state.text()
        assert user._retry_btn.isHidden() == (final_stop == "stop")
        assert window.chat._transcript.count() == len(window.chat._rows) + 1
    finally:
        await shutdown()


@pytest.mark.parametrize("old_text", ["", "部分输出"])
def test_run_error_never_inserts_a_transient_bubble(qtbot: QtBot, old_text: str) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("问题", "user-1")
    view.set_busy(True)
    user, assistant = view._rows
    state = assistant._run_state
    for attempt in (2, 3):
        view.end_assistant(
            old_text, "assistant-old", stop_reason="error", user_message_id="user-1"
        )
        view.add_error("连接失败", retry_message_id="user-1")
        # Check before retrying/settled: adding then deleting an error bubble is not reuse.
        assert view._rows == [user, assistant]
        assert view._transcript.count() == 3
        assert "连接失败" in state.text()
        assert not state.isHidden()
        view.add_retry_notice("连接失败", attempt=attempt, delay_ms=0, max_attempts=3)
        assert view._rows == [user, assistant]
        assert assistant._run_state is state
        assert f"第 {attempt}/3 次尝试" in state.text()
    view.end_assistant("成功", "assistant-new")
    view.set_busy(False)
    assert view._rows == [user, assistant]
    assert state.isHidden()


def test_empty_failed_reply_survives_settlement_and_manual_retry(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("问题", "user-1")
    view.set_busy(True)
    user, assistant = view._rows
    for attempt in range(1, 4):
        view.add_error("发送失败", retry_message_id=f"user-{attempt}")
        view.set_busy(False)
        assert view._rows == [user, assistant]
        assert "发送失败" in assistant._run_state.text()
        assert not assistant._run_state.isHidden()
        assert not assistant._segments[-1].hint._timer.isActive()
        assert not user._retry_btn.isHidden()
        view.retry_user_message("问题", f"user-{attempt + 1}", f"user-{attempt}")
        assert view._rows == [user, assistant]
        assert "发送失败" not in assistant._run_state.text()
    view.end_assistant("成功", "assistant-final")
    view.set_busy(False)
    assert view._rows == [user, assistant]
    assert assistant._run_state.isHidden()


@pytest.mark.parametrize("replay", ["stale", "missing", "empty"])
def test_unmatched_retry_never_becomes_a_new_send(qtbot: QtBot, replay: str) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    if replay != "empty":
        view.add_user_message("问题", "user-1")
        view.set_busy(True)
        view.end_assistant("失败", "assistant-1", stop_reason="error")
        view.set_busy(False)
        view.retry_user_message("问题", "user-2", "user-1")
        view.end_assistant("失败", "assistant-2", stop_reason="error")
        view.set_busy(False)
        view.retry_user_message("问题", "user-3", "user-2")
        view.append_assistant_delta("最新输出")
    rows = list(view._rows)
    tail = view._stream_row
    view.retry_user_message("问题", "user-2", "user-1" if replay == "stale" else "missing")
    assert view._rows == rows
    assert view._stream_row is tail
    if tail is not None:
        assert tail.content_text() == "最新输出"
    view.set_busy(False)


@pytest.mark.parametrize("stop", ["error", "aborted", "interrupted"])
def test_empty_incomplete_retry_never_constructs_another_bubble(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("问题", "user-0")
    view.set_busy(True)
    user, assistant = view._rows

    def forbid_new_bubble(*_args, **_kwargs):
        pytest.fail("重试必须复用原气泡，不允许创建新气泡")

    monkeypatch.setattr(view, "_add_bubble", forbid_new_bubble)
    for attempt in range(3):
        if stop == "interrupted":
            view.set_retry_available(f"user-{attempt}")
        else:
            view.end_assistant(
                "", f"assistant-{attempt}", stop_reason=stop,
                user_message_id=f"user-{attempt}",
            )
        view.set_busy(False)
        assert view._rows == [user, assistant]
        assert not assistant._run_state.isHidden()
        view.retry_user_message("问题", f"user-{attempt + 1}", f"user-{attempt}")
        assert view._rows == [user, assistant]
        assert view._stream_row is assistant
        view.begin_assistant()
    view.end_assistant("成功", "assistant-final")
    view.set_busy(False)
    assert view._rows == [user, assistant]
    assert assistant.content_text() == "成功"
