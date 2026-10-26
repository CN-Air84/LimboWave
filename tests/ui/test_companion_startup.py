"""Exercise qasync's real bootstrap lifecycle, not pytest's plain asyncio fixture."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("entry_point", ["button", "shortcut"])
def test_companion_opens_after_qasync_bootstrap(entry_point):
    # A fresh QApplication isolates aboutToQuit from pytest-qt's shared app.
    script = r"""
import asyncio
import sys
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from limbowave.ui.main_window import MainWindow
from limbowave.ui import theme

app = QApplication([])
app.setStyleSheet(theme.app_stylesheet())
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
window = MainWindow()
window.show()
quits = []
app.aboutToQuit.connect(lambda: quits.append(True))
try:
    # app._wire calls this to start IPC before entering the long-running loop.
    for _ in range(2):
        loop.run_until_complete(asyncio.sleep(0))
    assert len(quits) == 2
    window.activateWindow()
    window.chat._input.setFocus()
    QTest.qWait(30)
    if sys.argv[1] == "button":
        QTest.mouseClick(window.chat._companion_btn, Qt.MouseButton.LeftButton)
    else:
        QTest.keyClick(window.chat._input, Qt.Key.Key_M,
                      Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QTest.qWait(30)
    assert window._companion.active, (
        sys.argv[1], "did not open after bootstrap", window._companion._shutdown)
    mini = window._companion.window
    assert mini is not None and mini.isVisible()
    mini.close()
    assert window.isVisible() and not window._companion._shutdown
    window.close()
    assert window._companion._shutdown
finally:
    window.close()
    loop.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, entry_point],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
