"""Queue dispatch uses real app wiring and a local programmable kernel, never a provider."""

import asyncio
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QPushButton

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.bootstrap import AppPaths
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure import shell
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder
from limbowave.ui.main_window import MainWindow
from tests.ui.test_session_switch_during_generation import browse_history, wait_for
from tests.unit.test_branch_path import TreeKernel


@pytest.fixture
async def wired_queue(qtbot, qapp, monkeypatch, tmp_path, vault_key):
    stylesheet = qapp.styleSheet()
    kernel = TreeKernel()
    # Exercise catalog synchronization with a routed kernel, not an unconfigured
    # temporary controller (which legitimately has no catalog to synchronize).
    JsonConfigRepository(tmp_path / "config.json").save(AppConfiguration(
        endpoints=[EndpointConfig(
            id="fake", name="Fake", base_url="http://127.0.0.1:1/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        )],
        models=[LogicalModel(
            id="fake", name="Fake",
            bindings=[ModelBinding(endpoint_id="fake", model_id="fake")],
        )],
    ))
    monkeypatch.setattr(
        app, "_build_kernel", lambda *_a, **_kw: KernelSetup(
            kernel, "fake", "fake", "test", app_params={"model": "fake"}
        )
    )
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    catalog_calls = []

    def refresh(*_args, **_kwargs):
        catalog_calls.append(True)
        return SimpleNamespace(fingerprint=None)

    monkeypatch.setattr(EnvironmentBuilder, "refresh_catalog", refresh)

    class LocalIpc:
        session = rate_limit_session = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    def finish_bootstrap(coro):
        # _wire starts IPC synchronously before the real app loop. This fixture is
        # already async, so complete only our no-I/O startup stub synchronously.
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("Unexpected asynchronous work in IPC bootstrap stub")

    monkeypatch.setattr(app, "ToolIpcServer", LocalIpc)
    window = MainWindow()
    qtbot.addWidget(window)
    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(asyncio.get_running_loop(), "run_until_complete", finish_bootstrap)
        controller, _, _, _, shutdown = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), vault_key
        )
    window.chat.set_available(True)
    try:
        await controller.send("history question")
        kernel.say("history answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        first = (controller.conversation_id, controller.branch_id)
        window.sidebar.new_conversation_requested.emit()
        await wait_for(lambda: controller.conversation_id is None)
        await controller.send("other question")
        kernel.say("other answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        second = (controller.conversation_id, controller.branch_id)
        window.sidebar.new_conversation_requested.emit()
        await wait_for(lambda: controller.conversation_id is None)
        await controller.send("live question")
        yield window, controller, kernel, first, second, catalog_calls
    finally:
        await shutdown()
        qapp.setStyleSheet(stylesheet)


def enqueue(view, text):
    view._input.setPlainText(text)
    assert view._send_btn.isEnabled()
    view._send_btn.click()
    assert view._input.toPlainText() == ""


def entries(view):
    return view._queue_panel._signature[0]


async def settle(controller, kernel, text="done"):
    kernel.say(text)
    kernel.emit("run.settled", {})
    await controller.wait_idle()


async def test_fifo_sends_once_to_pinned_scopes_without_stealing_selected_view(wired_queue):
    window, controller, kernel, first, second, catalog_calls = wired_queue
    live = (controller.conversation_id, controller.branch_id)
    window.chat._input.setPlainText("keep live draft")
    await browse_history(window, first[0])
    enqueue(window.history_preview, "queued one")
    enqueue(window.history_preview, "queued two")
    await browse_history(window, second[0])
    enqueue(window.history_preview, "queued three")
    window.history_preview._input.setPlainText("new unsent draft")
    assert [text for text in kernel.sent if text.startswith("queued")] == []
    await settle(controller, kernel)
    await wait_for(lambda: "queued one" in kernel.sent)
    assert (controller.conversation_id, controller.branch_id) == first
    assert window.sidebar._active_conversation_id == second[0]
    assert window.history_preview._input.toPlainText() == "new unsent draft"
    assert all(row.content_text() != "queued one" for row in window.history_preview._rows)
    await settle(controller, kernel)
    await wait_for(lambda: "queued two" in kernel.sent)
    assert (controller.conversation_id, controller.branch_id) == first
    await settle(controller, kernel)
    await wait_for(lambda: "queued three" in kernel.sent)
    assert (controller.conversation_id, controller.branch_id) == second
    assert window.history_preview is None
    assert window.chat._input.toPlainText() == "new unsent draft"
    assert [x for x in kernel.sent if x.startswith("queued")] == [
        "queued one", "queued two", "queued three"
    ]
    assert len(catalog_calls) == 3
    await settle(controller, kernel)
    await browse_history(window, live[0])
    assert window.chat._input.toPlainText() == "keep live draft"


async def test_cancel_restores_text_and_never_sends_it(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    view = window.history_preview
    enqueue(view, "cancel me")
    key = entries(view)[0][1].id
    view._input.setPlainText("new draft")
    view._queue_panel.findChild(QPushButton, f"cancel_queue_{key}").click()
    assert view._input.toPlainText() == "new draft\ncancel me"
    await settle(controller, kernel)
    await wait_for(lambda: window.history_preview is None and not window.chat.history_loading)
    assert "cancel me" not in kernel.sent
    assert window.chat._input.toPlainText() == "new draft\ncancel me"


async def test_stop_pauses_remaining_queue_until_explicit_resume(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    enqueue(window.history_preview, "after stop")
    window.stop_requested.emit()
    await wait_for(lambda: kernel.aborted)
    await settle(controller, kernel)
    await asyncio.sleep(0.2)
    assert "after stop" not in kernel.sent
    view = window.history_preview or window.chat
    assert "已暂停" in view._queue_panel._summary.text()
    view._queue_panel._resume.click()
    await wait_for(lambda: "after stop" in kernel.sent)


async def test_failed_restore_retains_queue_and_requires_explicit_retry(wired_queue, monkeypatch):
    from limbowave.domain.runtime_state import RuntimeRestoreResult

    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    view = window.history_preview
    enqueue(view, "retry after restore")
    original = kernel.restore_runtime_state
    attempts = []

    async def fail_restore(_snapshot):
        attempts.append(True)
        return RuntimeRestoreResult(success=False, error="refused")

    monkeypatch.setattr(kernel, "restore_runtime_state", fail_restore)
    await settle(controller, kernel)
    await wait_for(lambda: entries(view) and entries(view)[0][1].state == "failed")
    await asyncio.sleep(0.2)
    assert len(attempts) == 1
    assert "retry after restore" not in kernel.sent
    assert entries(view)[0][1].text == "retry after restore"
    monkeypatch.setattr(kernel, "restore_runtime_state", original)
    view._queue_panel._resume.click()
    await wait_for(lambda: "retry after restore" in kernel.sent)
    assert kernel.sent.count("retry after restore") == 1


async def test_provider_failure_after_acceptance_does_not_requeue_message(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    enqueue(window.history_preview, "accepted once")
    enqueue(window.history_preview, "later message")
    kernel.send_error = RuntimeError("provider unavailable")
    await settle(controller, kernel)
    await wait_for(lambda: "已暂停" in window.chat._queue_panel._summary.text()
                   and not window.chat._busy)
    await controller.wait_idle()
    assert [item.text for _, item in entries(window.chat)] == ["later message"]
    assert [row.content_text() for row in window.chat._rows].count("accepted once") == 1
    kernel.send_error = None
    window.chat._queue_panel._resume.click()
    await wait_for(lambda: "later message" in kernel.sent)
    assert "accepted once" not in kernel.sent


async def test_busy_live_conversation_can_queue_without_losing_new_draft(wired_queue):
    window, controller, kernel, _, _, _ = wired_queue
    live = (controller.conversation_id, controller.branch_id)
    enqueue(window.chat, "live follow up")
    window.chat._input.setPlainText("next unsent draft")
    await settle(controller, kernel)
    await wait_for(lambda: "live follow up" in kernel.sent)
    assert (controller.conversation_id, controller.branch_id) == live
    assert window.history_preview is None
    assert window.chat._input.toPlainText() == "next unsent draft"


async def test_queue_keeps_branch_identity_even_if_selected_branch_changes(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    await settle(controller, kernel)
    await browse_history(window, first[0])
    await controller.edit_user_message(window.chat._rows[0]._message_id, "edited")
    await settle(controller, kernel)
    new_branch = controller.branch_id
    assert new_branch != first[1]
    window.sidebar.new_conversation_requested.emit()
    await wait_for(lambda: controller.conversation_id is None)
    await controller.send("running elsewhere")

    async def select(branch):
        window.sidebar.branch_switch_requested.emit(first[0], branch)
        await wait_for(lambda: window.sidebar._active_branch_id == branch
                       and not window.chat.history_loading)

    await select(first[1])
    enqueue(window.history_preview, "old branch request")
    await select(new_branch)
    enqueue(window.history_preview, "new branch request")
    await settle(controller, kernel)
    await wait_for(lambda: "old branch request" in kernel.sent)
    assert controller.branch_id == first[1]
    await settle(controller, kernel)
    await wait_for(lambda: "new branch request" in kernel.sent)
    assert controller.branch_id == new_branch


async def test_missing_target_never_falls_back_to_live_conversation(wired_queue):
    from limbowave.application.services.history_service import HistoryService

    window, controller, kernel, first, _, _ = wired_queue
    live_id = controller.conversation_id
    await browse_history(window, first[0])
    view = window.history_preview
    enqueue(view, "target was removed")
    history = HistoryService(controller.coordinator()._uow_factory)
    assert await asyncio.to_thread(history.delete, first[0])
    await settle(controller, kernel)
    await wait_for(lambda: entries(view)[0][1].state == "failed")
    assert controller.conversation_id == live_id
    assert "target was removed" not in kernel.sent
    assert entries(view)[0][1].text == "target was removed"


async def test_context_limit_pauses_before_accepting_queued_message(wired_queue, monkeypatch):
    from limbowave.application.services.compression_service import CompressionService

    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    enqueue(window.history_preview, "too much context")

    async def blocked(*_args, **_kwargs):
        return SimpleNamespace(action=SimpleNamespace(value="block"), usage=None, display="blocked")

    monkeypatch.setattr(CompressionService, "estimate", blocked)
    await settle(controller, kernel)
    await wait_for(lambda: entries(window.chat) and entries(window.chat)[0][1].state == "failed")
    assert "too much context" not in kernel.sent
    assert "上下文" in window.chat._queue_panel._summary.text()


async def test_dispatch_preserves_live_view_when_user_returns_before_settlement(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    live_id = controller.conversation_id
    await browse_history(window, first[0])
    enqueue(window.history_preview, "background send")
    await browse_history(window, live_id)
    window.chat._input.setPlainText("stay on live draft")
    await settle(controller, kernel, "old live completed")
    await wait_for(lambda: "background send" in kernel.sent)
    assert window.sidebar._active_conversation_id == live_id
    assert window.history_preview is not None
    assert window.history_preview._input.toPlainText() == "stay on live draft"
    assert window.history_preview._rows[-1].content_text() == "old live completed"
    assert controller.conversation_id == first[0]


@pytest.mark.parametrize("during_render", [False, True])
async def test_settlement_during_navigation_waits_and_preserves_selection(
    wired_queue, monkeypatch, during_render
):
    from limbowave.ui.chat_view import ChatView

    window, controller, kernel, first, second, _ = wired_queue
    await browse_history(window, first[0])
    enqueue(window.history_preview, "send after navigation")
    entered, release = asyncio.Event(), asyncio.Event()
    if during_render:
        original = ChatView.load_history_incrementally

        async def slow_render(view, *args, **kwargs):
            if view._draft_only:
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
    window.sidebar.conversation_selected.emit(second[0])
    await asyncio.wait_for(entered.wait(), 2)
    await settle(controller, kernel)
    await asyncio.sleep(0.15)
    assert "send after navigation" not in kernel.sent
    release.set()
    await wait_for(lambda: "send after navigation" in kernel.sent)
    assert window.sidebar._active_conversation_id == second[0]
    assert controller.conversation_id == first[0]
    assert kernel.sent.count("send after navigation") == 1


async def test_repeated_click_does_not_queue_the_cleared_composer_twice(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    view = window.history_preview
    enqueue(view, "one click only")
    view._send_btn.click()
    view.message_queued.emit("one click only")  # Stale queued UI signal.
    assert len(entries(view)) == 1
    await settle(controller, kernel)
    await wait_for(lambda: "one click only" in kernel.sent)
    assert kernel.sent.count("one click only") == 1


async def test_cancel_does_not_silently_replace_an_edit_draft(wired_queue):
    window, _, _, _, _, _ = wired_queue
    enqueue(window.chat, "queued follow up")
    key = entries(window.chat)[0][1].id
    window.chat.begin_edit(window.chat._rows[0]._message_id, "editing existing message")
    editing_id = window.chat.editing_message_id
    window.chat.queue_cancel_requested.emit(key)
    assert window.chat.editing_message_id == editing_id
    assert window.chat._input.toPlainText() == "editing existing message"
    assert len(entries(window.chat)) == 1


async def test_stale_enqueue_signal_never_erases_newer_draft(wired_queue):
    window, _, _, _, _, _ = wired_queue
    window.chat._input.setPlainText("newer draft")
    window.chat.message_queued.emit("stale draft")
    assert window.chat._input.toPlainText() == "newer draft"
    assert not entries(window.chat)


@pytest.mark.parametrize("interruption", ["runtime", "aborted"])
async def test_runtime_or_remote_abort_pauses_pending_requests(wired_queue, interruption):
    window, controller, kernel, first, _, _ = wired_queue
    await browse_history(window, first[0])
    enqueue(window.history_preview, "wait for recovery")
    if interruption == "runtime":
        await controller.mark_interrupted("runtime unavailable")
    else:
        kernel.say("", stop="aborted")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
    await asyncio.sleep(0.3)
    assert "wait for recovery" not in kernel.sent
    view = window.history_preview or window.chat
    assert "已暂停" in view._queue_panel._summary.text()


async def test_deleted_active_scope_is_checked_even_when_no_restore_is_needed(wired_queue):
    from limbowave.application.services.history_service import HistoryService

    window, controller, kernel, _, _, _ = wired_queue
    live_id = controller.conversation_id
    enqueue(window.chat, "do not send into deleted live scope")
    window.stop_requested.emit()
    await settle(controller, kernel)
    await asyncio.sleep(0.15)
    history = HistoryService(controller.coordinator()._uow_factory)
    assert await asyncio.to_thread(history.delete, live_id)
    window.chat._queue_panel._resume.click()
    await wait_for(lambda: entries(window.chat)[0][1].state == "failed")
    assert "do not send into deleted live scope" not in kernel.sent
    assert "已不存在" in window.chat._queue_panel._summary.text()
