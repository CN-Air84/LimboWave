"""Real Qt/qasync restore lifecycle in an isolated subprocess and temporary library."""
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
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog
from limbowave import app
from limbowave.application.kernel import KernelSetup
from limbowave.application.services.backup_service import BackupService
from limbowave.application.services.restore_service import PreparedRestore
from limbowave.bootstrap import AppContext, AppPaths
from limbowave.infrastructure import shell
from limbowave.infrastructure.database.migrations import CURRENT_VERSION
from limbowave.ui.settings_panel import SettingsPage
from tests.unit.test_restore_service import library, title, KDF
from tests.unit.test_run_coordinator import FakeKernel

base = Path(sys.argv[1])
scenario = sys.argv[2]
source = base / 'source'
source_key = library(source, 'restored')
archive = base / 'backup.lwb'
BackupService(source, app_version='test', schema_version=CURRENT_VERSION,
              kdf_params=KDF, vault_key=source_key).create(archive, 'backup password')
root = base / 'live'
key = library(root, 'original')
paths = AppPaths(root, root / 'logs')
events = []
failures = []
storages = []
ipcs = []

class Kernel(FakeKernel):
    async def set_memory_context(self, context):
        from limbowave.domain.memory_presets import BUILTIN_READER_PRESET_CONTENT
        assert BUILTIN_READER_PRESET_CONTENT in context["global"]
        await super().set_memory_context(context)
    async def abort(self):
        events.append('abort')
        await super().abort()
    async def shutdown(self):
        events.append('shutdown')
        assert title(root, 'original password') == 'original'
        if scenario == 'shutdown-fails':
            raise RuntimeError('simulated shutdown failure')
        await super().shutdown()

app.create_context = lambda: AppContext(paths, 'test', False)
app._unlock_vault = lambda *_: key
app._configuration_needs_onboarding = lambda *_: False
app._build_kernel = lambda *a, **kw: KernelSetup(Kernel(), 'fake', 'fake', 'test')
shell.probe_shell = lambda *_: None
app._show_startup_alert = lambda parent, heading, message: events.append('alert:' + message)
QFileDialog.getOpenFileName = lambda *a, **kw: (str(archive), '')
app.ask_prompt = lambda parent, heading, prompt, callback, **kw: callback('backup password')

def confirm(parent, heading, text, callback, **kwargs):
    if heading == '停止生成并退出？':
        assert scenario == 'cancel'
        events.append('close-confirm')
        QTimer.singleShot(0, lambda: callback(True))
        return
    assert '自动保留' in text and '备份密码解锁' in text
    events.append('confirm')
    callback(scenario != 'cancel')
    if scenario == 'cancel':
        parent.close()

app.ask_confirm = confirm
real_factory = app.sqlite_uow_factory

def factory(*args):
    storage = real_factory(*args)
    storages.append(storage)
    return storage

app.sqlite_uow_factory = factory
real_execute = PreparedRestore.execute

def execute(prepared):
    events.append('execute')
    assert 'shutdown' in events
    assert all(store._closed and not store._units for store in storages)
    assert all(ipc.session is None for ipc in ipcs)
    if scenario == 'install-fails':
        raise OSError('simulated preflight failure')
    return real_execute(prepared)

PreparedRestore.execute = execute

def restart(*args, **kwargs):
    events.append('restart')
    assert title(root, 'backup password') == 'restored'
    assert not (root / '.restore-pending.json').exists()
    if scenario == 'launch-fails':
        raise OSError('simulated relaunch failure')

app.restart_application = restart
real_wire = app._wire

def wire(window, *args, **kwargs):
    result = real_wire(window, *args, **kwargs)
    controller, ipc, *_ = result
    if ipc is not None:
        ipcs.append(ipc)
    async def interact():
        try:
            await asyncio.sleep(0.1)
            while window.transition_active:
                await asyncio.sleep(0.02)
            await controller.send('active run')
            assert controller.busy
            window.sidebar.settings_requested.emit()
            page = window.findChild(SettingsPage)
            assert page is not None
            page.restore_requested.emit()
        except BaseException as exc:
            failures.append(repr(exc))
            asyncio.get_running_loop().stop()
    asyncio.ensure_future(interact())
    return result

app._wire = wire
code = app.main(['limbowave'])
assert not failures, failures
assert all(store._closed and not store._units for store in storages)
if scenario == 'cancel':
    assert code == 0, (code, events)
    assert 'execute' not in events and 'restart' not in events
    assert title(root, 'original password') == 'original'
    assert not list((root / 'restore-history').iterdir())
elif scenario in ('shutdown-fails', 'install-fails'):
    assert code == 1, (code, events)
    assert 'restart' not in events
    assert title(root, 'original password') == 'original'
    assert ('execute' in events) == (scenario == 'install-fails')
else:
    assert code == (1 if scenario == 'launch-fails' else 0), (code, events)
    assert events.index('abort') < events.index('shutdown') < events.index('execute')
    assert events.index('execute') < events.index('restart')
    assert title(root, 'backup password') == 'restored'
    previous = next((root / 'restore-history').glob('*/previous'))
    assert title(previous, 'original password') == 'original'
print('RESTORE_LIFECYCLE_OK')
'''


@pytest.mark.parametrize('scenario', [
    'success', 'cancel', 'shutdown-fails', 'install-fails', 'launch-fails',
])
def test_restore_lifecycle(tmp_path: Path, scenario: str) -> None:
    # Storage workers can inherit stdout; a file keeps timeout cleanup from waiting
    # forever for a surviving worker to close a captured pipe after a failed GUI exit.
    log = tmp_path / 'restore-process.log'
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run(
            [sys.executable, '-c', SCRIPT, str(tmp_path), scenario],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONIOENCODING': 'utf-8'},
            stdout=output, stderr=subprocess.STDOUT, timeout=45, check=False,
        )
    transcript = log.read_text(encoding='utf-8', errors='replace')
    assert result.returncode == 0, transcript
    assert 'RESTORE_LIFECYCLE_OK' in transcript
