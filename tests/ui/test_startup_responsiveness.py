"""Login startup must leave Qt free to animate while doing blocking I/O."""

from __future__ import annotations

import asyncio
import builtins
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from qasync import QEventLoop

from limbowave import app
from limbowave.application.services.history_service import HistoryService
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure import shell
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow


def test_startup_task_keeps_qt_alive_and_propagates_errors(qtbot):
    gui_thread = threading.get_ident()
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(threading.get_ident()))

    def work():
        assert threading.get_ident() != gui_thread
        time.sleep(0.1)
        raise ValueError("startup failed")

    timer.start()
    try:
        with pytest.raises(ValueError, match="startup failed"):
            app._run_startup_task(work)
    finally:
        timer.stop()
    assert len(ticks) >= 3
    assert set(ticks) == {gui_thread}


def test_legacy_secret_migration_runs_off_gui_thread(qtbot, tmp_path, vault_key, monkeypatch):
    gui_thread = threading.get_ident()
    threads = []

    class UnlockedVault:
        exists = True

        def unlock(self, password):
            return vault_key

    monkeypatch.setattr(app, "Vault", lambda path: UnlockedVault())
    monkeypatch.setattr(app, "_ask_password", lambda *args: "password")
    monkeypatch.setattr(
        app, "migrate_legacy_secrets", lambda *args: threads.append(threading.get_ident())
    )
    host = QWidget()
    qtbot.addWidget(host)
    assert app._unlock_vault(AppPaths(tmp_path, tmp_path / "logs"), host) is vault_key
    assert len(threads) == 1
    assert threads[0] != gui_thread


def test_wiring_offloads_io_and_reads_history_once(qtbot, tmp_path, vault_key, monkeypatch):
    gui_thread = threading.get_ident()
    calls = []
    ticks = Counter()
    phase = [""]

    def slow(name, fn):
        def run(*args, **kwargs):
            calls.append((name, threading.get_ident()))
            phase[0] = name
            try:
                time.sleep(0.09)
                return fn(*args, **kwargs)
            finally:
                phase[0] = ""
        return run

    monkeypatch.setattr(app, "sqlite_uow_factory", slow("storage", app.sqlite_uow_factory))
    monkeypatch.setattr(app, "_build_kernel", slow("kernel", lambda *args, **kwargs: None))
    monkeypatch.setattr(shell, "probe_shell", slow("shell", lambda *args: None))
    monkeypatch.setattr(
        HistoryService, "list_conversations", slow("history", HistoryService.list_conversations)
    )
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()
    page._begin_loading()
    ui_threads = []
    show_conversations = window.sidebar.show_conversations

    def show(rows):
        ui_threads.append(threading.get_ident())
        show_conversations(rows)

    monkeypatch.setattr(window.sidebar, "show_conversations", show)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.update([phase[0]]))
    timer.start()
    try:
        result = app._wire(window, AppPaths(tmp_path, tmp_path / "logs"), vault_key)
        loop.run_until_complete(result[-1]())
    finally:
        timer.stop()
        loop.close()
        asyncio.set_event_loop(None)

    assert {name for name, _ in calls} == {"storage", "kernel", "shell", "history"}
    assert all(thread != gui_thread for _, thread in calls), calls
    assert all(ticks[name] >= 2 for name, _ in calls), ticks
    assert Counter(name for name, _ in calls)["history"] == 1
    assert ui_threads == [gui_thread]
    assert page._blackout.opacity == 1.0


def test_prepared_appearance_is_not_reapplied(qtbot, tmp_path: Path, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(shell, "probe_shell", lambda *args: None)

    def reapply(*args):
        pytest.fail("appearance was already prepared before login")

    monkeypatch.setattr(window, "set_appearance_theme", reapply)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    try:
        result = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), None, appearance_prepared=True
        )
        loop.run_until_complete(result[-1]())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_instant_startup_results_do_not_lose_completion(qtbot):
    expected = object()
    for _ in range(30):
        assert app._run_startup_task(lambda: expected) is expected


def test_settings_import_is_deferred_and_widgets_are_built_on_gui_thread(
    qtbot, tmp_path, vault_key, monkeypatch
):
    gui_thread = threading.get_ident()
    imported_on = []
    original_import = builtins.__import__

    def record_import(name, *args, **kwargs):
        if name == "limbowave.ui.settings_panel":
            imported_on.append(threading.get_ident())
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", record_import)
    monkeypatch.setattr(shell, "probe_shell", lambda *args: None)
    window = MainWindow()
    qtbot.addWidget(window)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    shutdown = None
    try:
        _, _, warm, _, shutdown = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), vault_key,
            appearance_prepared=True,
        )
        assert imported_on == []
        warm()
        assert imported_on == [gui_thread]
        settings = window.findChild(QWidget, "settingsPage")
        assert settings is not None
        assert settings.thread() is window.thread()
        warm()
        assert imported_on == [gui_thread], "prewarming must reuse the page"
    finally:
        if shutdown is not None:
            loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)
