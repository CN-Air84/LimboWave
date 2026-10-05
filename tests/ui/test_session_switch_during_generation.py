"""Navigation must not replace the kernel context or destroy a live reply."""

import asyncio

import pytest

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.ui.main_window import MainWindow
from tests.unit.test_branch_path import TreeKernel


async def wait_for(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


@pytest.fixture
async def wired(qtbot, qapp, monkeypatch, tmp_path):
    stylesheet = qapp.styleSheet()
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
    window.chat.set_available(True)
    try:
        # An existing conversation to browse while another one streams.
        await controller.send("history question")
        kernel.say("history answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        history_id, history_branch = controller.conversation_id, controller.branch_id
        window.sidebar.new_conversation_requested.emit()
        await wait_for(lambda: controller.conversation_id is None)
        await controller.send("other question")
        kernel.say("other answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        other_id = controller.conversation_id
        window.sidebar.new_conversation_requested.emit()
        await wait_for(lambda: controller.conversation_id is None)
        await controller.send("live question")
        yield window, controller, kernel, history_id, history_branch, other_id
    finally:
        await shutdown()
        qapp.setStyleSheet(stylesheet)


async def browse_history(window, history_id):
    window.sidebar.conversation_selected.emit(history_id)
    await wait_for(lambda: window.sidebar._active_conversation_id == history_id
                   and not window.chat.history_loading)


async def test_browse_does_not_interrupt_or_redirect_live_reply(wired):
    window, controller, kernel, history_id, _, _ = wired
    live_id, live_branch = controller.conversation_id, controller.branch_id
    live_rows = list(window.chat._rows)
    window.chat._input.setPlainText("keep my draft")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit("message.update", {
        "assistantMessageEvent": {"type": "text_delta", "delta": "before switch"}
    })
    await browse_history(window, history_id)
    preview = window.history_preview
    assert preview is not None
    assert [row.content_text() for row in preview._rows] == ["history question", "history answer"]
    assert controller.conversation_id == live_id
    assert controller.branch_id == live_branch
    assert controller.busy
    assert not kernel.aborted
    assert not kernel.restored
    assert not preview._input.isEnabled()
    kernel.emit("message.update", {
        "assistantMessageEvent": {"type": "text_delta", "delta": " after switch"}
    })
    await asyncio.sleep(0.1)
    assert [row.content_text() for row in preview._rows] == ["history question", "history answer"]
    assert window.sidebar._active_conversation_id == history_id
    window.sidebar.conversation_selected.emit(live_id)
    await wait_for(lambda: window.history_preview is None)
    assert window.chat._rows == live_rows
    assert window.chat._rows[-1].content_text() == "before switch after switch"
    assert window.chat._input.toPlainText() == "keep my draft"
    assert window.chat._busy and window.chat._stop_btn.isEnabled()
    assert window.sidebar._active_conversation_id == live_id


async def test_background_completion_activates_displayed_history(wired):
    window, controller, kernel, history_id, history_branch, _ = wired
    await browse_history(window, history_id)
    kernel.say("background answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: controller.conversation_id == history_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert controller.branch_id == history_branch
    assert [row.content_text() for row in window.chat._rows] == [
        "history question", "history answer"
    ]
    assert not window.chat._busy
    assert window.chat._input.isEnabled()
    await controller.send("follow up in history")
    assert controller.conversation_id == history_id
    assert [row.content_text() for row in window.chat._rows][:3] == [
        "history question", "history answer", "follow up in history"
    ]


@pytest.mark.parametrize("during_render", [False, True])
async def test_completion_during_navigation_does_not_leave_a_read_only_view(
    wired, monkeypatch, during_render
):
    from limbowave.ui.chat_view import ChatView

    window, controller, kernel, history_id, _, _ = wired
    entered, release = asyncio.Event(), asyncio.Event()
    if during_render:
        original = ChatView.load_history_incrementally

        async def slow_render(view, *args, **kwargs):
            if view._read_only:
                entered.set()
                await release.wait()
            return await original(view, *args, **kwargs)

        monkeypatch.setattr(ChatView, "load_history_incrementally", slow_render)
    else:
        original = app.HistoryReader.read

        async def slow_read(reader, operation, *args, **kwargs):
            result = await original(reader, operation, *args, **kwargs)
            if operation.__name__ == "_read_history_view":
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(app.HistoryReader, "read", slow_read)
    window.sidebar.conversation_selected.emit(history_id)
    await asyncio.wait_for(entered.wait(), 2)
    kernel.say("finished during navigation")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    release.set()
    await wait_for(lambda: controller.conversation_id == history_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert window.chat._input.isEnabled()
    assert window.sidebar._active_conversation_id == history_id


async def test_current_live_selection_is_a_noop_and_stop_still_targets_live_run(wired):
    window, controller, kernel, history_id, _, _ = wired
    live_id = controller.conversation_id
    rows = list(window.chat._rows)
    window.sidebar.conversation_selected.emit(live_id)
    await asyncio.sleep(0.05)
    assert window.history_preview is None
    assert window.chat._rows == rows
    await browse_history(window, history_id)
    window.sidebar.conversation_selected.emit(live_id)
    await wait_for(lambda: window.history_preview is None)
    window.chat._stop_btn.click()
    await wait_for(lambda: kernel.aborted)
    assert controller.conversation_id == live_id


async def test_invalid_history_keeps_preview_and_can_return_to_live(wired):
    window, controller, _kernel, history_id, _, _ = wired
    live_id = controller.conversation_id
    await browse_history(window, history_id)
    preview = window.history_preview
    window.sidebar.conversation_selected.emit("missing-conversation")
    await wait_for(lambda: not window.chat.history_loading and len(preview._rows) == 3)
    assert window.sidebar._active_conversation_id == history_id
    assert controller.conversation_id == live_id
    assert not preview._composer.isEnabled()
    window.sidebar.conversation_selected.emit(live_id)
    await wait_for(lambda: window.history_preview is None)
    assert window.chat._busy


def test_read_only_history_keeps_paging_and_blocks_all_mutations(qtbot):
    from limbowave.ui.chat_view import ChatView, HistoryEntry

    view = ChatView(read_only=True)
    qtbot.addWidget(view)
    entries = [
        HistoryEntry("user", f"question {index}", "", f"u{index}", retry_available=True)
        for index in range(150)
    ]
    view.load_history(entries)
    view.set_history_loading(True)
    view.set_history_loading(False)
    before = len(view._rows)
    view._load_earlier()
    assert len(view._rows) > before
    assert not view._composer.isEnabled()
    assert not view._input.isEnabled()
    for row in view._rows:
        assert not row._edit_btn.isEnabled()
        assert not row._retry_btn.isEnabled()


async def test_multiple_history_selections_only_activate_the_last_one(wired):
    window, controller, kernel, history_id, _, other_id = wired
    live_id = controller.conversation_id
    await browse_history(window, history_id)
    await browse_history(window, other_id)
    assert [row.content_text() for row in window.history_preview._rows] == [
        "other question", "other answer"
    ]
    assert controller.conversation_id == live_id
    kernel.say("live answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: controller.conversation_id == other_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert [row.content_text() for row in window.chat._rows] == ["other question", "other answer"]
    assert window.sidebar._active_conversation_id == other_id


async def test_branch_navigation_and_restore_failure_keep_runtime_isolated(wired, monkeypatch):
    from limbowave.domain.runtime_state import RuntimeRestoreResult

    window, controller, kernel, history_id, history_branch, _ = wired
    live_id = controller.conversation_id
    window.sidebar.branch_switch_requested.emit(history_id, history_branch)
    await wait_for(lambda: window.sidebar._active_branch_id == history_branch
                   and not window.chat.history_loading)
    assert controller.conversation_id == live_id
    assert not kernel.restored

    async def fail_restore(_snapshot):
        return RuntimeRestoreResult(success=False, error="restore refused")

    monkeypatch.setattr(kernel, "restore_runtime_state", fail_restore)
    kernel.say("background answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: not window.chat.history_loading
                   and len(window.history_preview._rows) == 3)
    assert controller.conversation_id == live_id
    assert window.sidebar._active_branch_id == history_branch
    assert not window.history_preview._composer.isEnabled()
    # No automatic retry loop, and a failed restore cannot send to the wrong context.
    await asyncio.sleep(0.1)
    assert len(window.history_preview._rows) == 3


def test_preview_stack_layout_and_live_view_restoration(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.chat.add_user_message("live question", "live-u")
    window.chat.set_busy(True)
    window.chat.append_assistant_delta("still generating")
    preview = window.prepare_history_preview()
    preview.load_history([("user", "history question", "", "old-u"),
                          ("assistant", "history answer", "", "old-a")])
    window.show()
    window.show_history_preview()
    qtbot.wait(100)
    assert preview.isVisible() and not window.chat.isVisible()
    assert not window.toolbar.isVisible()
    window.resize(1000, 700)
    qtbot.wait(100)
    assert preview.size() == window._chat_stack.size()
    assert window.grab().save(str(tmp_path / "history-preview.png"))
    window.clear_history_preview()
    qtbot.wait(100)
    assert window.chat.isVisible()
    assert window.chat._busy
    assert window.chat._rows[-1].content_text() == "still generating"


async def test_completion_during_failed_navigation_activates_previous_selection(wired, monkeypatch):
    window, controller, kernel, history_id, _, _ = wired
    await browse_history(window, history_id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.HistoryReader.read

    async def slow_missing_read(reader, operation, *args, **kwargs):
        result = await original(reader, operation, *args, **kwargs)
        if operation.__name__ == "_read_history_view" and args[0] == "missing":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(app.HistoryReader, "read", slow_missing_read)
    window.sidebar.conversation_selected.emit("missing")
    await asyncio.wait_for(entered.wait(), 2)
    kernel.say("background answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    release.set()
    await wait_for(lambda: controller.conversation_id == history_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert window.chat._input.isEnabled()
