"""GUI 的诊断入口、Qt 消息桥接与真实进程冒烟。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QtMsgType, qCritical, qDebug, qInfo, qInstallMessageHandler, qWarning
from pytestqt.qtbot import QtBot

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.diagnostics import (
    DiagnosticRuntime,
    LogConfig,
    LogLevel,
    LogManager,
    LogReader,
)
from limbowave.ui.diagnostics_runtime import DiagnosticsWindow, QtDiagnosticsBridge
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow
from limbowave.ui.settings_panel import SettingsPage


def test_qt_levels_and_previous_handler_restore(tmp_path: Path) -> None:
    observed = []

    def previous(kind, context, message):
        observed.append(kind)

    original = qInstallMessageHandler(previous)
    try:
        with LogManager(LogConfig(tmp_path, level="debug")) as manager:
            with QtDiagnosticsBridge(manager) as bridge:
                qDebug("debug")
                qInfo("info")
                qWarning("password=secret-from-qt")
                qCritical("critical")
                # 只调用桥接器，不调用会终止测试进程的 qFatal。
                bridge._handle(
                    QtMsgType.QtFatalMsg,
                    SimpleNamespace(
                        category="test",
                        file="test.py",
                        line=1,
                        function="test",
                    ),
                    "fatal",
                )
                assert manager.flush()
                result = manager.recent()
                assert [entry.level for entry in result] == list(LogLevel)
                assert "secret-from-qt" not in repr(result)
            restored = qInstallMessageHandler(previous)
            assert restored is previous
    finally:
        qInstallMessageHandler(original)
    assert len(observed) == 5


def test_login_shortcut_reuses_window_and_capture_level(qtbot: QtBot, tmp_path: Path) -> None:
    import logging

    with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
        window = MainWindow()
        qtbot.addWidget(window)
        window.show_full_page(LoginPage())
        dialog = DiagnosticsWindow(runtime.manager, window, on_level_changed=runtime.set_level)
        window.diagnostics_requested.connect(dialog.show_diagnostics)
        window.show()
        window.activateWindow()
        qtbot.waitExposed(window)
        qtbot.waitUntil(window.isActiveWindow)
        qtbot.keyClick(
            window,
            Qt.Key.Key_L,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        qtbot.waitUntil(dialog.isVisible)
        panel = dialog._panel
        assert panel is not None
        assert panel._timer.isActive()
        panel._capture_level.setCurrentText("DEBUG")
        logging.getLogger("limbowave.test").debug("debug now collected")
        panel.refresh()
        assert any(entry.message == "debug now collected" for entry in panel._model.entries)
        dialog.close()
        assert not panel._timer.isActive()
        window.diagnostics_requested.emit()
        assert dialog._panel is panel
        assert dialog.isVisible()
        window.close()
        assert not dialog.isVisible()
        assert not panel._timer.isActive()


def test_settings_button_emits_request(qtbot: QtBot, tmp_path: Path, vault_key) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    page = SettingsPage(None, settings, credentials, diagnostics_available=True)
    qtbot.addWidget(page)
    with qtbot.waitSignal(page.diagnostics_requested):
        page._diagnostics_button.click()


def test_memory_mode_is_visible(qtbot: QtBot, tmp_path: Path) -> None:
    with LogManager(LogConfig(tmp_path / "absent", file_enabled=False)) as manager:
        parent = MainWindow()
        qtbot.addWidget(parent)
        dialog = DiagnosticsWindow(manager, parent)
        dialog.show_diagnostics()
        assert dialog._panel is not None
        assert "仅内存" in dialog._panel._location.text()
        assert "退出后不会保留" in dialog._panel._status.text()
        dialog.close()


def _smoke(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "limbowave",
            "--smoke",
            "--log-level",
            "debug",
            "--log-dir",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
    )


def test_actual_smoke_writes_lifecycle_to_chinese_path(tmp_path: Path) -> None:
    path = tmp_path / "中文诊断日志"
    result = _smoke(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[smoke] ok" in result.stdout
    entries = list(LogReader(LogConfig(path)).iter_entries())
    assert any(entry.message == "application.startup" for entry in entries)
    assert any(entry.message == "application.shutdown" for entry in entries)
    # 子进程已释放目标锁。
    with LogManager(LogConfig(path)):
        pass


def test_actual_smoke_survives_log_target_lock(tmp_path: Path) -> None:
    with LogManager(LogConfig(tmp_path)) as owner:
        owner.get_logger("owner").info("only owner")
        assert owner.flush()
        result = _smoke(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "仅内存" in result.stderr
        assert [entry.message for entry in LogReader(owner.config).iter_entries()] == ["only owner"]


def test_gui_startup_exception_restores_handlers_and_closes_writer(tmp_path: Path) -> None:
    script = """
import logging, sys, threading
from pathlib import Path
from limbowave import app
from limbowave.bootstrap import AppContext, AppPaths
root = Path(sys.argv[1])
app.create_context = lambda: AppContext(AppPaths(root / "data", root / "logs"), "test", False)
def fail(*args):
    raise RuntimeError("synthetic startup failure")
app._unlock_vault = fail
previous = tuple(logging.getLogger("limbowave").handlers)
try:
    app.main(["test"])
except RuntimeError:
    pass
else:
    raise AssertionError("startup should fail")
assert tuple(logging.getLogger("limbowave").handlers) == previous
assert not any(t.name.startswith("diagnostics-") for t in threading.enumerate())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    entries = list(LogReader(LogConfig(tmp_path / "logs")).iter_entries())
    assert any(entry.message == "application.unhandled" for entry in entries)
