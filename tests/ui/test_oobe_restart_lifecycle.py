"""Real Qt/qasync restart lifecycle, isolated from the user's data and app process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = r"""
import asyncio
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from limbowave import app, bootstrap
from limbowave.application.kernel import KernelSetup
from limbowave.bootstrap import AppContext, AppPaths
from limbowave.infrastructure import shell
from limbowave.infrastructure.crypto.vault import Vault
from limbowave.infrastructure.diagnostics import LogConfig, LogManager
from limbowave.ui import oobe_live
from limbowave.ui.oobe_demo import OobeDemo
from tests.conftest import TEST_KDF, TEST_PASSWORD
from tests.unit.test_run_coordinator import FakeKernel

root = Path(sys.argv[1])
scenario = sys.argv[2]
paths = AppPaths(root, root / "logs")
key = Vault(root / "vault.json", params=TEST_KDF).create(TEST_PASSWORD)
(root / "config.json").write_text('{"endpoints":[],"models":[]}', encoding="utf-8")
original_config = (root / "config.json").read_bytes()
original_vault = (root / "vault.json").read_bytes()
events = []
failures = []
stores = []
ipcs = []
windows = []

class Kernel(FakeKernel):
    async def shutdown(self):
        events.append("shutdown")
        if scenario == "shutdown-fails":
            raise RuntimeError("test shutdown failure")
        await super().shutdown()

kernel = Kernel()
release = scenario.startswith("release-")
installed = scenario == "installed-forced"
app.create_context = lambda: AppContext(paths, "test", release)
if installed:
    bootstrap.is_development_environment = lambda: False

def unlock(_paths, window):
    events.append("unlock")
    if scenario == "login-restart":
        window.oobe_restart_requested.emit()
    return None if scenario == "locked" else key

app._unlock_vault = unlock
app._configuration_needs_onboarding = lambda _paths: scenario in ("normal-empty", "release-empty")
app._build_kernel = lambda *_args, **_kwargs: KernelSetup(kernel, "fake", "fake", "test")
shell.probe_shell = lambda *_args: None
app._show_startup_alert = lambda _parent, title, _detail: events.append("alert:" + title)
real_factory = app.sqlite_uow_factory

def factory(*args):
    store = real_factory(*args)
    stores.append(store)
    return store

app.sqlite_uow_factory = factory
real_dispose = app._dispose_gui_loop

def dispose(loop):
    real_dispose(loop)
    assert loop.is_closed()
    events.append("disposed")

app._dispose_gui_loop = dispose

real_onboarding = oobe_live.run_onboarding

def onboarding(window, *args):
    events.append("oobe")
    def interact():
        try:
            page = window.findChild(OobeDemo)
            assert page is not None and page.screen_id == "intro"
            if scenario == "oobe-restart":
                window.oobe_restart_requested.emit()
                window.oobe_restart_requested.emit()
            elif scenario == "oobe-close":
                window.close()
            else:
                page.skipped.emit()
        except BaseException as exc:
            failures.append(repr(exc))
            window.close()
    QTimer.singleShot(20, interact)
    real_onboarding(window, *args)

oobe_live.run_onboarding = onboarding
real_wire = app._wire

def wire(window, *args, **kwargs):
    events.append("wire")
    windows.append(window)
    result = real_wire(window, *args, **kwargs)
    controller, ipc, *_rest = result
    if ipc is not None:
        ipcs.append(ipc)

    def interact():
        try:
            if scenario in ("release-forced", "installed-forced"):
                from PySide6.QtGui import QKeySequence
                assert not any(
                    action.shortcut() == QKeySequence("Ctrl+Shift+O")
                    for action in window.actions()
                )
                window.oobe_restart_requested.emit()
                assert window.isVisible(), "release must reject debug restart intents"
                window.close()
            elif scenario == "close-refused":
                class GuardedPage(QWidget):
                    def resolve_pending_changes(self):
                        return False
                window.show_full_page(GuardedPage())
                window.oobe_restart_requested.emit()
                assert window.isVisible(), "refused close must cancel the restart"
                window.show_workspace()
                window.close()
            elif scenario in ("restart", "shutdown-fails", "launch-fails"):
                window.oobe_restart_requested.emit()
                window.oobe_restart_requested.emit()
            else:
                window.close()
        except BaseException as exc:
            failures.append(repr(exc))
            asyncio.get_event_loop().stop()
    QTimer.singleShot(150, interact)
    return result

app._wire = wire

def relaunch(argv, *, frozen):
    assert "disposed" in events
    assert all(store._closed and not store._units for store in stores)
    assert all(ipc.session is None for ipc in ipcs)
    assert not any(window.isVisible() for window in windows)
    if stores:
        assert "shutdown" in events
    # The replacement process must not inherit a locked diagnostics target.
    with LogManager(LogConfig(paths.log_root)):
        pass
    assert argv[1:3] == ["--log-level", "DEBUG"], argv
    assert not frozen
    events.append("relaunch")
    if scenario == "launch-fails":
        raise OSError("test launch failure")

app.restart_into_oobe = relaunch
forced = scenario in (
    "forced-skip", "oobe-restart", "oobe-close", "locked", "release-forced", "installed-forced"
)
argv = ["limbowave", "--log-level", "DEBUG", *(["--oobe"] if forced else [])]
try:
    code = app.main(argv)
except RuntimeError as exc:
    assert scenario == "shutdown-fails" and "test shutdown failure" in str(exc)
    code = 1

assert not failures, failures
assert (root / "config.json").read_bytes() == original_config
assert (root / "vault.json").read_bytes() == original_vault
assert all(store._closed for store in stores)
assert all(ipc.session is None for ipc in ipcs)
expected_restart = scenario in ("restart", "oobe-restart", "login-restart", "launch-fails")
assert events.count("relaunch") == int(expected_restart), events
expected_oobe = (
    forced and scenario != "locked" and not release and not installed
) or scenario in ("normal-empty", "release-empty")
assert events.count("oobe") == int(expected_oobe), events
assert ("wire" in events) is (scenario not in ("oobe-restart", "oobe-close", "login-restart"))
assert code == (1 if scenario in ("shutdown-fails", "launch-fails") else 0), (code, events)
if scenario == "launch-fails":
    assert "alert:重启失败" in events
print("OOBE_RESTART_OK", events)
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "restart",
        "forced-skip",
        "normal",
        "normal-empty",
        "oobe-restart",
        "oobe-close",
        "locked",
        "shutdown-fails",
        "close-refused",
        "launch-fails",
        "login-restart",
        "release-forced",
        "installed-forced",
        "release-empty",
    ],
)
def test_oobe_restart_lifecycle(tmp_path: Path, scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path), scenario],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=35,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OOBE_RESTART_OK" in result.stdout
