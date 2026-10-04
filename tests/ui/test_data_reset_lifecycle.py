"""真实 Qt/qasync 主循环的重置退出回归，独立进程 + 临时数据目录。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = r'''
import asyncio
import sys
from pathlib import Path
from unittest.mock import Mock

from PySide6.QtWidgets import QPushButton

from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.application.services import data_reset_service as reset
from limbowave.bootstrap import AppContext, AppPaths
from limbowave.infrastructure import shell
from limbowave.infrastructure.crypto.vault import Vault
from limbowave.ui import data_reset_panel as reset_ui
from limbowave.ui.data_reset_panel import DataResetPanel
from limbowave.ui.settings_panel import SettingsPage
from tests.conftest import TEST_KDF, TEST_PASSWORD
from tests.unit.test_run_coordinator import FakeKernel

root = Path(sys.argv[1])
scenario = sys.argv[2]
paths = AppPaths(root, root / "logs")
key = Vault(root / "vault.json", params=TEST_KDF).create(TEST_PASSWORD)
(root / "backup.lwb").write_bytes(b"keep backup")
events = []
failures = []
storages = []
ipcs = []
tasks = []

class Kernel(FakeKernel):
    async def abort(self):
        events.append("abort")
        await super().abort()

    async def shutdown(self):
        events.append("kernel_shutdown")
        assert (root / "limbowave.db").is_file()
        if scenario == "shutdown-fails":
            raise RuntimeError("shutdown failed")
        await super().shutdown()

kernel = Kernel()
app.create_context = lambda: AppContext(paths, "test", False)
app._unlock_vault = lambda *_args: key
app._build_kernel = lambda *_args, **_kwargs: KernelSetup(kernel, "fake", "fake", "test")
shell.probe_shell = lambda *_args: None
reset_ui.supports_system_identity = lambda: True  # exercise fake Windows verification on any host
app._show_startup_alert = lambda _parent, _title, detail: events.append("alert:" + detail)
gate = Mock()
gate.name = "windows-hello"
gate.available.return_value = True
gate.verify.return_value = True
reset.hello_gate = lambda: gate
real_factory = app.sqlite_uow_factory

def factory(*args):
    value = real_factory(*args)
    storages.append(value)
    return value

app.sqlite_uow_factory = factory
real_execute = reset.DataResetService.execute

def execute(service):
    events.append("delete")
    assert "kernel_shutdown" in events
    assert all(store._closed and not store._units for store in storages)
    assert all(ipc.session is None for ipc in ipcs)
    if scenario == "delete-fails":
        raise reset.DataResetError("test deletion failed")
    real_execute(service)

reset.DataResetService.execute = execute
real_wire = app._wire

def wire(window, *args, **kwargs):
    result = real_wire(window, *args, **kwargs)
    controller, ipc, *_rest = result
    if ipc is not None:
        ipcs.append(ipc)

    async def interact():
        try:
            while window.transition_active:
                await asyncio.sleep(0.02)
            # 保持一轮运行进行中，证明重置会先中止它，再清理数据库。
            await controller.send("temporary active run")
            assert controller.busy
            window.sidebar.settings_requested.emit()
            page = window.findChild(SettingsPage)
            assert page is not None
            page.select_tab("数据")
            page.findChild(QPushButton, "resetDataButton").click()
            panel = window.findChild(DataResetPanel)
            assert panel is not None
            panel._scope.setCurrentIndex(1 if scenario == "all" else 0)
            panel._password.setText(TEST_PASSWORD)
            panel._verify.click()
            while panel._deadline is None:
                await asyncio.sleep(0.02)
            if scenario == "cancel":
                panel._cancel.click()
                window.close()
                return
            assert not panel._confirm.isEnabled()
            started = asyncio.get_running_loop().time()
            while not panel._confirm.isEnabled():
                await asyncio.sleep(0.02)
            assert asyncio.get_running_loop().time() - started >= 4.95
            assert (root / "limbowave.db").is_file()
            panel._confirm.click()
        except BaseException as exc:
            failures.append(repr(exc))
            asyncio.get_running_loop().stop()

    async def start_later():
        # 等到 main 已创建 _startup，并给它完成首次同步的机会。
        await asyncio.sleep(0.05)
        await interact()

    tasks.append(asyncio.ensure_future(start_later()))
    return result

app._wire = wire
code = app.main(["limbowave"])
assert not failures, failures
assert all(store._closed and not store._units for store in storages)
assert (root / "backup.lwb").read_bytes() == b"keep backup"
if scenario in ("shutdown-fails", "delete-fails"):
    assert code == 1, (code, events)
    assert (root / "limbowave.db").exists()
    assert any(event.startswith("alert:") for event in events)
    assert ("delete" in events) is (scenario == "delete-fails")
elif scenario == "cancel":
    assert code == 0
    assert "delete" not in events
    assert (root / "limbowave.db").exists()
else:
    assert code == 0, (code, events)
    assert events.index("abort") < events.index("kernel_shutdown") < events.index("delete")
    assert not (root / "limbowave.db").exists()
    assert (root / "vault.json").exists() is (scenario == "database")
    if scenario == "all":
        assert not (root / "themes").exists()
print("RESET_LIFECYCLE_OK")
'''


@pytest.mark.parametrize(
    "scenario", ["database", "all", "cancel", "shutdown-fails", "delete-fails"],
)
def test_application_reset_lifecycle(tmp_path: Path, scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path), scenario],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESET_LIFECYCLE_OK" in result.stdout
