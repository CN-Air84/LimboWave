"""Qt timer must continue while the regeneration worker waits for database I/O."""

import asyncio
import time
from itertools import pairwise

import pytest
from PySide6.QtCore import QTimer

from limbowave.application.services.history_service import HistoryService
from limbowave.infrastructure.database.sqlite_repositories import open_connection
from tests.unit.test_branch_path import _turn
from tests.unit.test_conversation_process import process_stack as process_stack


async def test_qt_timer_continues_during_regeneration(qapp, process_stack):
    coord, kernel, factory, worker, database = process_stack
    await worker.warm()
    await _turn(kernel, coord, "question", "answer")
    target = HistoryService(factory).branch_messages(coord.branch_id)[-1]
    lock = open_connection(database)
    lock.execute("BEGIN IMMEDIATE")
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    task = asyncio.create_task(coord.regenerate(target.id))
    try:
        for _ in range(40):
            qapp.processEvents()
            await asyncio.sleep(0.005)
        assert len(ticks) >= 15
        assert max(b - a for a, b in pairwise(ticks)) < 0.15
        assert not task.done()  # the worker is still waiting for the writer lock
    finally:
        timer.stop()
        lock.rollback()
        lock.close()
    assert await task
    kernel.say("new answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


def test_persistent_app_uses_process_and_never_reloads_prefix(
    qtbot, qapp, monkeypatch, tmp_path, vault_key
):
    from qasync import QEventLoop

    from limbowave import app
    from limbowave.application.kernel import KernelSetup
    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure import shell
    from limbowave.ui.main_window import MainWindow
    from tests.unit.test_branch_path import TreeKernel

    kernel = TreeKernel()
    monkeypatch.setattr(
        app, "_build_kernel", lambda *_a, **_kw: KernelSetup(kernel, "fake", "fake", "test")
    )
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    old_stylesheet = qapp.styleSheet()
    loop = QEventLoop(qapp)
    asyncio.set_event_loop(loop)
    window = MainWindow()
    qtbot.addWidget(window)
    controller, _ipc, _warm, startup, shutdown = app._wire(
        window, AppPaths(tmp_path, tmp_path / "logs"), vault_key
    )

    async def drive():
        coord = controller.coordinator()
        assert coord.storage_worker is not None
        await startup()
        await _turn(kernel, coord, "first", "first answer")
        await _turn(kernel, coord, "second", "old answer")
        retained = window.chat._rows[:2]

        def forbidden(*args, **kwargs):
            raise AssertionError("GUI rescanned history on regenerate")

        monkeypatch.setattr(HistoryService, "branch_messages", forbidden)
        with coord._uow_factory() as uow:
            monkeypatch.setattr(type(uow.snapshots), "list_all_intents", forbidden)
        window.chat._rows[-1]._regenerate_btn.click()
        window.chat._rows[-1]._regenerate_btn.click()  # disabled immediately
        async with asyncio.timeout(10):
            while len(kernel.sent) < 3:
                await asyncio.sleep(0.005)
        assert kernel.sent == ["first", "second", "second"]
        assert window.chat._rows[:2] == retained
        kernel.say("new answer")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        assert window.chat._rows[-1].content_text() == "new answer"

        # If the visible paging window cannot be reused, do not let fast new tokens
        # arrive before the incremental prefix load has completed.
        entered = asyncio.Event()
        release = asyncio.Event()
        render = window.chat.load_history_incrementally

        async def delayed_render(messages):
            entered.set()
            await release.wait()
            return await render(messages)

        monkeypatch.setattr(window.chat, "apply_branch_history", lambda *_args: False)
        monkeypatch.setattr(window.chat, "load_history_incrementally", delayed_render)
        window.chat._rows[-1]._regenerate_btn.click()
        await asyncio.wait_for(entered.wait(), 10)
        assert len(kernel.sent) == 3
        assert coord.busy and window.chat.history_loading
        assert not await coord.new_session()
        release.set()
        async with asyncio.timeout(10):
            while len(kernel.sent) < 4:
                await asyncio.sleep(0.005)
        kernel.say("latest answer")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        assert not window.chat.history_loading
        assert window.chat._rows[-1].content_text() == "latest answer"

    try:
        loop.run_until_complete(drive())
    finally:
        loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)
        qapp.setStyleSheet(old_stylesheet)


@pytest.mark.parametrize("operation", ["fork", "retry"])
async def test_qt_timer_continues_during_fork_and_retry(qapp, process_stack, operation):
    coord, kernel, factory, worker, database = process_stack
    await worker.warm()
    await _turn(kernel, coord, "question", "answer")
    messages = HistoryService(factory).branch_messages(coord.branch_id)
    lock = open_connection(database)
    lock.execute("BEGIN IMMEDIATE")
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    task = asyncio.create_task(
        coord.fork_message(messages[-1].id) if operation == "fork"
        else coord.retry_user_message(messages[0].id)
    )
    try:
        for _ in range(40):
            qapp.processEvents()
            await asyncio.sleep(.005)
        assert len(ticks) >= 15
        assert max(b-a for a,b in pairwise(ticks)) < .15
        assert not task.done()
    finally:
        timer.stop()
        lock.rollback()
        lock.close()
    assert await task
    if operation == "retry":
        kernel.say("new answer")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
