"""A new chat can configure its memories before sending any message."""

import asyncio
import threading

import pytest

import limbowave.app as app
from limbowave.application.kernel import KernelSetup
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService
from limbowave.bootstrap import AppPaths
from limbowave.domain.memory_presets import BUILTIN_READER_PRESET_CONTENT
from limbowave.infrastructure import shell
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from limbowave.ui.main_window import MainWindow
from limbowave.ui.session_memory_panel import MemoryPanel
from tests.unit.test_run_coordinator import FakeKernel


async def wait_for(qapp, predicate):
    for _ in range(200):
        qapp.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


@pytest.mark.parametrize("with_kernel", [True, False])
@pytest.mark.parametrize("window_height", [800, 480])
async def test_new_chat_memory_opens_before_first_send(
    qtbot, qapp, monkeypatch, tmp_path, with_kernel, window_height,
):
    class MemoryKernel(FakeKernel):
        async def set_memory_context(self, context):
            self.memory_prompt = context

    kernel = MemoryKernel()
    monkeypatch.setattr(
        app, "_build_kernel",
        lambda *_a, **_kw: KernelSetup(kernel, "fake", "fake", "test") if with_kernel else None,
    )
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    factory = in_memory_uow_factory()
    monkeypatch.setattr(app, "in_memory_uow_factory", lambda: factory)
    service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json")),
    )
    original = service.save("global original")
    gui_thread = threading.get_ident()
    initialization_threads = []
    initialize = MemoryService.ensure_builtin_preset

    def track_initialization(self):
        initialization_threads.append(threading.get_ident())
        return initialize(self)

    monkeypatch.setattr(MemoryService, "ensure_builtin_preset", track_initialization)
    stylesheet = qapp.styleSheet()
    window = MainWindow()
    qtbot.addWidget(window)
    controller, _ipc, _warm, _startup, shutdown = app._wire(
        window, AppPaths(tmp_path, tmp_path / "logs"), None,
    )
    window.resize(1000, window_height)
    window.show()
    try:
        assert len(initialization_threads) == 1
        assert initialization_threads[0] != gui_thread
        assert len(service.list()) == 2
        preset = service.list()[1]
        assert preset.content == BUILTIN_READER_PRESET_CONTENT
        assert controller.conversation_id is None
        window.chat._input.setPlainText("unsent draft")
        window._toolbar._memory_btn.click()
        await wait_for(qapp, lambda: bool(window.findChildren(MemoryPanel)))
        panel = window.findChildren(MemoryPanel)[-1]
        await wait_for(qapp, panel.isEnabled)
        outer = panel.parentWidget()
        await wait_for(qapp, lambda: outer._motion is None)
        assert outer.minimumWidth() == 620
        assert outer.height() >= min(560, window.height() - 28)
        assert window.rect().contains(outer.geometry())
        window._toolbar._memory_btn.click()
        window._toolbar._memory_btn.click()
        await asyncio.sleep(0.05)
        qapp.processEvents()
        active_panels = [
            view for view in window.findChildren(MemoryPanel)
            if not view._floating_parent()._closing
        ]
        assert active_panels == [panel], "Repeated clicks should reuse the same memory panel"
        conversation_id, branch_id = controller.conversation_id, controller.branch_id
        assert conversation_id and branch_id
        assert (panel.conversation_id, panel.branch_id) == (conversation_id, branch_id)
        assert window.chat._input.toPlainText() == "unsent draft"
        assert kernel.sent == []
        with factory() as uow:
            assert uow.messages.list_for_branch(branch_id) == []
            assert uow.runs.list_for_conversation(conversation_id) == []
        service.save_session_global(original.id, "before-first-message", conversation_id)
        panel._floating_parent().close_panel()
        window._toolbar._memory_btn.click()
        await asyncio.sleep(0.1)
        assert controller.conversation_id == conversation_id
        if with_kernel:
            events = []
            controller.subscribe(events.append)
            assert await controller.send("first message")
            assert controller.conversation_id == conversation_id
            assert kernel.memory_prompt["global"] == ["before-first-message", preset.content]
            kernel.say("reply")
            kernel.emit("run.settled", {})
            await controller.wait_idle()
            assert any(event.kind == "first_response_completed" for event in events)
        window.sidebar.new_conversation_requested.emit()
        await wait_for(qapp, lambda: controller.conversation_id is None)
        window._toolbar._memory_btn.click()
        await wait_for(qapp, lambda: controller.conversation_id is not None)
        assert controller.conversation_id != conversation_id
        assert service.effective_global(controller.conversation_id) == [original, preset]
        assert service.list() == [original, preset]
    finally:
        await shutdown()
        qapp.setStyleSheet(stylesheet)
