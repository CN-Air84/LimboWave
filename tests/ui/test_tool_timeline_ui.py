"""Regression through real app event wiring: live details and reload ordering."""

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.application.services.history_service import HistoryService
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.ui.main_window import MainWindow
from tests.unit.test_branch_path import TreeKernel


def timeline(row):
    return [
        (s.raw_text, tuple(step.tool_call_id for step in s.live_tool_steps)) for s in row._segments
    ]


@pytest.mark.parametrize("check_details", [True, False])
async def test_live_tools_survive_history_reload(qtbot, monkeypatch, tmp_path, check_details):
    kernel = TreeKernel()
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
        await controller.send("read notes")
        kernel.say("checking notes", stop="toolUse")
        payload = {"toolCallId": "call-1", "toolName": "read", "args": {"path": "notes.md"}}
        kernel.emit("tool.start", payload)
        row = window.chat._rows[-1]
        steps_view = row._tool_steps_view
        chip = steps_view.findChild(QPushButton)
        chip.click()
        detail = steps_view.findChild(QLabel, "detail-call-1")
        if check_details:
            assert "notes.md" in detail.text()
        controller.coordinator()._live.tool_started_at["call-1"] -= 0.25
        kernel.emit("tool.end", {**payload, "result": {"output": "found notes"}})
        if check_details:
            assert row._tool_steps_view is steps_view
            assert not detail.isHidden()
            assert "found notes" in detail.text()
            assert "耗时：0ms" not in detail.text()
        kernel.say("final answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        before = timeline(row)
        history = HistoryService(controller.coordinator()._uow_factory).branch_messages(
            controller.coordinator().branch_id
        )
        window.chat.load_history(app._history_payload(history))
        assert timeline(window.chat._rows[-1]) == before
        assert window.chat._rows[-1].content_text() == "checking notes\n\nfinal answer"
    finally:
        await shutdown()


def test_late_tool_result_updates_its_original_open_detail(qtbot):
    from limbowave.domain.tool_step import ToolStep, finish
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("checking", "message")
    running = ToolStep("call-1", "read", args={"path": "notes.md"})
    view.note_tool_step("read", "call-1", phase="start", is_error=False, step=running)
    row = view._rows[-1]
    first = row._segments[0]
    chip = first.tool_steps_view.findChild(QPushButton)
    chip.click()
    detail = first.tool_steps_view.findChild(QLabel, "detail-call-1")
    view.begin_assistant()
    view.end_assistant("answer", "message")
    done = finish(running, result={"error": "not found"}, is_error=True, duration_ms=123)
    view.note_tool_step("read", "call-1", phase="end", is_error=True, step=done)
    assert first.live_tool_steps == [done]
    assert row._segments[1].tool_steps_view is None
    assert not detail.isHidden()
    assert "not found" in detail.text()
    assert "123ms" in detail.text()
    view.set_busy(False)


def test_legacy_tool_steps_precede_final_answer(qtbot):
    from limbowave.domain.tool_step import ToolStep
    from limbowave.ui.chat_view import ChatView, HistoryEntry

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [HistoryEntry("assistant", "final answer", "", "old", "run", (ToolStep("call-1", "read"),))]
    )
    part = view._rows[-1]._segments[0]
    assert part.column.indexOf(part.tool_steps_view) < part.column.indexOf(part.content)


def test_updating_tool_details_preserves_other_expansions_and_hidden_state(qtbot):
    from limbowave.domain.tool_step import ToolStep, finish
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.begin_assistant()
    first = ToolStep("call-1", "read", args={"path": "notes.md"})
    second = ToolStep("call-2", "search", args={"query": "notes"})
    view.note_tool_step("read", "call-1", phase="start", is_error=False, step=first)
    steps = view._rows[-1]._tool_steps_view
    steps.findChild(QPushButton).click()
    detail = steps.findChild(QLabel, "detail-call-1")
    view.note_tool_step("search", "call-2", phase="start", is_error=False, step=second)
    assert not detail.isHidden()
    assert len(steps.findChildren(QPushButton)) == 2
    view.set_tool_steps_visible(False)
    done = finish(first, result="done", duration_ms=99)
    view.note_tool_step("read", "call-1", phase="end", is_error=False, step=done)
    assert steps.isHidden()
    view.set_tool_steps_visible(True)
    assert not steps.isHidden()
    assert not detail.isHidden()
    assert "done" in detail.text()
