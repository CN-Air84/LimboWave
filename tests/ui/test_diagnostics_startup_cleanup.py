"""装配中途失败也应先回收业务资源，不能只关闭诊断写者。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from qasync import QEventLoop

from limbowave import app
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure.diagnostics import DiagnosticRuntime, LogConfig
from limbowave.ui.main_window import MainWindow


def test_partial_wiring_failure_closes_storage_and_ipc(
    qtbot: QtBot, tmp_path: Path, vault_key, monkeypatch,
) -> None:
    stores = []
    servers = []
    original_store = app.sqlite_uow_factory
    original_ipc = app.ToolIpcServer

    def store(*args):
        result = original_store(*args)
        stores.append(result)
        return result

    def server(*args, **kwargs):
        result = original_ipc(*args, **kwargs)
        servers.append(result)
        return result

    def fail(*args, **kwargs):
        raise RuntimeError("failed after IPC startup")

    monkeypatch.setattr(app, "sqlite_uow_factory", store)
    monkeypatch.setattr(app, "ToolIpcServer", server)
    monkeypatch.setattr(app, "_build_kernel", fail)
    window = MainWindow()
    qtbot.addWidget(window)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    try:
        with DiagnosticRuntime(LogConfig(tmp_path / "logs")), pytest.raises(RuntimeError, match="after IPC"):
            app._wire(window, AppPaths(tmp_path, tmp_path / "logs"), vault_key)
        assert len(stores) == len(servers) == 1
        assert stores[0]._closed
        assert servers[0].session is None
        assert not asyncio.all_tasks(loop)
    finally:
        loop.close()
        asyncio.set_event_loop(None)
