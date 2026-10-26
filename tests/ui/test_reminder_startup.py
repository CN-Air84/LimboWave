"""Reminder tasks must not run in the nested Qt-only startup loops."""

from __future__ import annotations

import asyncio
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from limbowave import app
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.reminder_scheduler import ReminderScheduler
from limbowave.application.services.reminder_service import ReminderService
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.ui.main_window import MainWindow


@pytest.mark.parametrize("unlocked", [True, False], ids=["unlocked", "locked"])
def test_reminders_wait_for_asyncio_start_and_stop_with_app(
    qtbot, tmp_path, vault_key, monkeypatch, caplog, unlocked,
):
    gui_thread = threading.get_ident()
    history_entered, release_history = threading.Event(), threading.Event()
    history_ticks = []
    scheduler_loops, scheduler_tasks, scan_threads, errors = [], [], [], []
    original_history = HistoryService.list_conversations
    original_run = ReminderScheduler.run
    original_deliver = ReminderService.deliver_due

    def slow_history(service):
        history_entered.set()
        assert release_history.wait(timeout=5), "Qt must keep processing startup events"
        return original_history(service)

    def release_after_qt_ticks():
        if history_entered.is_set():
            history_ticks.append(True)
            if len(history_ticks) >= 3:
                release_history.set()

    async def observe_scheduler(scheduler):
        # Record before accessing a running loop, so premature execution is visible.
        scheduler_loops.append(loop.is_running())
        scheduler_tasks.append(asyncio.current_task(loop=loop))
        scheduler._interval = 0.01
        await original_run(scheduler)

    def observe_scan(service):
        result = original_deliver(service)
        scan_threads.append(threading.get_ident())
        return result

    monkeypatch.setattr(HistoryService, "list_conversations", slow_history)
    monkeypatch.setattr(ReminderScheduler, "run", observe_scheduler)
    monkeypatch.setattr(ReminderService, "deliver_due", observe_scan)
    monkeypatch.setattr(shell, "probe_shell", lambda *args: None)
    monkeypatch.setattr(app, "_build_kernel", lambda *args, **kwargs: None)
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(window.chat, "add_error", errors.append)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(release_after_qt_ticks)
    timer.start()
    shutdown = None
    try:
        controller, _, _, _, shutdown = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), vault_key if unlocked else None,
            appearance_prepared=True,
        )
        timer.stop()
        assert len(history_ticks) >= 3
        assert scheduler_loops == [], "reminder task ran before qasync started"
        assert errors == []
        assert not controller.available

        async def exercise_runtime():
            # Do not start a model: local reminders must be independent of Pi startup.
            if unlocked:
                async with asyncio.timeout(5):
                    while len(scan_threads) < 2:
                        await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.03)

        loop.run_until_complete(exercise_runtime())
        assert scheduler_loops == ([True] if unlocked else [])
        assert all(thread != gui_thread for thread in scan_threads)
        if not unlocked:
            assert scan_threads == []
    finally:
        release_history.set()
        timer.stop()
        if shutdown is not None:
            loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)

    assert all(task is not None and task.cancelled() for task in scheduler_tasks)
    assert errors == []
    assert "background.operation_failed" not in caplog.text
    assert "reminder.scan_failed" not in caplog.text
