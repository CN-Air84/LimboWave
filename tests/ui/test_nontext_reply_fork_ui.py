"""Click Fork on a tool-only/thinking-only card through the real app wiring."""

import asyncio

import pytest

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.ui.main_window import MainWindow
from tests.unit.test_branch_path import TreeKernel
from tests.unit.test_nontext_reply_fork import emit_nontext_reply
from tests.unit.test_runtime_message_anchors import NormalizingKernel


@pytest.mark.parametrize("kind", ["tools", "thinking"])
async def test_nontext_card_fork_preserves_visible_content(qtbot, monkeypatch, tmp_path, kind):
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
        await controller.send("读取文档")
        emit_nontext_reply(kernel, kind)
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        original_branch = controller.coordinator().branch_id
        card = window.chat._rows[-1]
        assert card._role == "assistant"
        assert card.has_content()
        assert not card._fork_btn.isHidden()
        target_id = card._message_id
        card._fork_btn.click()
        for _ in range(100):
            if controller.coordinator().branch_id != original_branch:
                break
            await asyncio.sleep(0.01)
        assert controller.coordinator().branch_id != original_branch
        assert kernel.sent == ["读取文档"]
        assert not window.chat._busy
        assert [row._role for row in window.chat._rows] == ["user", "assistant"]
        restored_card = window.chat._rows[-1]
        assert restored_card._message_id == target_id
        assert restored_card.has_content()
        assert restored_card.content_text() == ""
        assert not restored_card._fork_btn.isHidden()
        assert not restored_card._regenerate_btn.isHidden()
    finally:
        await shutdown()


async def test_normalized_prompt_card_buttons_can_fork_and_regenerate(qtbot, monkeypatch, tmp_path):
    kernel = NormalizingKernel()
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
        await controller.send("image question")
        kernel.say("original answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        original_branch = controller.coordinator().branch_id
        window.chat._rows[-1]._fork_btn.click()
        async with asyncio.timeout(10):
            while controller.coordinator().branch_id == original_branch:
                await asyncio.sleep(0.01)
        assert kernel.sent == ["image question"]
        window.chat._rows[-1]._regenerate_btn.click()
        async with asyncio.timeout(10):
            while len(kernel.sent) < 2:
                await asyncio.sleep(0.01)
        kernel.say("new answer")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        assert window.chat._rows[-1].content_text() == "new answer"
        assert kernel.sent == ["image question", "image question"]
    finally:
        await shutdown()
