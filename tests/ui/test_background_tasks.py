import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from limbowave.ui.background_tasks import BackgroundTasks


def test_worker_keeps_qt_live_and_delivers_on_gui(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    jobs = BackgroundTasks(widget)
    ticks, results = [], []
    timer = QTimer(widget)
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    gui_thread = threading.get_ident()

    def work():
        time.sleep(.2)
        return threading.get_ident()

    jobs.submit(work, lambda thread: results.append((thread, threading.get_ident())),
                lambda exc: results.append(exc))
    qtbot.waitUntil(lambda: bool(results))
    assert results[0][0] != gui_thread
    assert results[0][1] == gui_thread
    assert len(ticks) >= 5


def test_worker_error_delivered_on_gui(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    jobs = BackgroundTasks(widget)
    errors = []
    jobs.submit(lambda: 1 / 0, lambda _: None, errors.append)
    qtbot.waitUntil(lambda: bool(errors))
    assert isinstance(errors[0], ZeroDivisionError)
