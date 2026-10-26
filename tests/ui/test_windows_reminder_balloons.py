import ctypes
import sys
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from limbowave.ui.windows_reminder_balloons import (
    CALLBACK_MESSAGE,
    NIIF_NOSOUND,
    NIIF_RESPECT_QUIET_TIME,
    NIM_ADD,
    NIM_DELETE,
    NIM_MODIFY,
    NIM_SETVERSION,
    NIN_BALLOONUSERCLICK,
    NotifyIconData,
    WindowsReminderBalloons,
)


@pytest.fixture
def balloons(qtbot):
    window = QWidget()
    qtbot.addWidget(window)
    backend = WindowsReminderBalloons(window)
    records, destroyed = [], []
    def notify(operation, pointer):
        data = ctypes.cast(pointer, ctypes.POINTER(NotifyIconData)).contents
        records.append((operation, NotifyIconData.from_buffer_copy(bytes(data))))
        return True
    backend._shell = SimpleNamespace(Shell_NotifyIconW=notify)
    backend._user = SimpleNamespace(DestroyIcon=destroyed.append)
    backend._icon = 42  # Native APIs are fake: never pass a real handle to them.
    yield backend, records, destroyed
    backend.close()


def test_payload_silences_windows_default_sound_and_obeys_bounds(balloons):
    backend, records, _ = balloons
    assert backend.show("title" * 50, "💧" * 300, lambda: None)
    assert [r[0] for r in records] == [NIM_ADD, NIM_SETVERSION, NIM_MODIFY]
    data = records[-1][1]
    assert data.dwInfoFlags & NIIF_NOSOUND
    assert data.dwInfoFlags & NIIF_RESPECT_QUIET_TIME
    assert len(data.szInfoTitle.encode("utf-16-le")) <= 63 * 2
    assert len(data.szInfo.encode("utf-16-le")) <= 255 * 2
    assert data.cbSize == ctypes.sizeof(NotifyIconData)
    if sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8:
        assert data.cbSize == 976


def test_old_balloon_click_uses_old_target_not_latest(balloons, qtbot):
    backend, records, _ = balloons
    clicked = []
    backend.show("first", "body", lambda: clicked.append("first"))
    first = records[-1][1].uID
    backend.show("second", "body", lambda: clicked.append("second"))
    second = records[-1][1].uID
    native = wintypes.MSG()
    native.hWnd = backend._hwnd
    native.message = CALLBACK_MESSAGE
    native.lParam = (first << 16) | NIN_BALLOONUSERCLICK
    assert backend.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(native)) == (True, 0)
    qtbot.waitUntil(lambda: clicked == ["first"])
    assert first not in backend._callbacks and second in backend._callbacks
    native.hWnd = backend._hwnd + 1
    assert not backend.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(native))[0]
    backend.close()
    native.hWnd = backend._hwnd
    native.lParam = (second << 16) | NIN_BALLOONUSERCLICK
    backend.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(native))
    qtbot.wait(10)
    assert clicked == ["first"]


def test_bounded_icons_cleanup_and_failure(balloons):
    backend, records, destroyed = balloons
    for i in range(12):
        backend.show(str(i), "body", lambda: None)
    assert len(backend._callbacks) == len(backend._timers) == 8
    backend.close()
    assert not backend._callbacks and not backend._timers
    assert sum(operation == NIM_DELETE for operation, _ in records) == 12
    assert destroyed == [42]
    backend.close()
    assert destroyed == [42]


def test_failed_show_removes_registered_icon(balloons):
    backend, records, _ = balloons
    original = backend._shell.Shell_NotifyIconW
    def fail(operation, pointer):
        original(operation, pointer)
        return operation != NIM_MODIFY
    backend._shell.Shell_NotifyIconW = fail
    assert not backend.show("title", "body", lambda: None)
    assert records[-1][0] == NIM_DELETE
    assert not backend._callbacks and not backend._timers
