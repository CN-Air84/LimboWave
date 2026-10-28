"""Windows-native silent balloons; each icon owns its click target, never the live chat."""
from __future__ import annotations

import ctypes
import itertools
import sys
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from limbowave.ui.app_icon import application_icon

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16
NIIF_INFO, NIIF_NOSOUND, NIIF_RESPECT_QUIET_TIME = 1, 16, 128
NIN_BALLOONHIDE, NIN_BALLOONTIMEOUT, NIN_BALLOONUSERCLICK = 0x403, 0x404, 0x405
CALLBACK_MESSAGE = 0x8517  # Private WM_APP message, scoped to our HWND.
_IDS = itertools.count(0x6200)


class _Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid), ("hBalloonIcon", wintypes.HICON),
    ]


def _utf16_prefix(text: str, capacity: int) -> str:
    return text.encode("utf-16-le")[:(capacity - 1) * 2].decode("utf-16-le", errors="ignore")


class WindowsReminderBalloons(QAbstractNativeEventFilter):
    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self._window = window
        window.destroyed.connect(self.close)
        self._shell: Any = None
        self._user: Any = None
        self._icon = 0
        self._hwnd = 0
        self._installed = False
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._timers: dict[int, QTimer] = {}
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            self._shell = ctypes.WinDLL("shell32", use_last_error=True)
            self._shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                                    ctypes.POINTER(NotifyIconData)]
            self._shell.Shell_NotifyIconW.restype = wintypes.BOOL
            self._user = ctypes.WinDLL("user32", use_last_error=True)
            self._user.DestroyIcon.argtypes = [wintypes.HICON]
            self._user.DestroyIcon.restype = wintypes.BOOL

    def _data(self, identity: int) -> NotifyIconData:
        value = NotifyIconData()
        value.cbSize = ctypes.sizeof(value)
        value.hWnd, value.uID = self._hwnd, identity
        return value

    def show(self, title: str, body: str, on_click: Callable[[], None]) -> bool:
        if self._shell is None:
            return False
        if not self._icon:
            icon = application_icon()
            self._icon = int(icon.pixmap(32, 32).toImage().toHICON())
            if not self._icon:
                return False
        self._hwnd = int(self._window.winId())
        app = QApplication.instance()
        if not self._installed and app is not None:
            app.installNativeEventFilter(self)
            self._installed = True
        while len(self._callbacks) >= 8:
            self._remove(next(iter(self._callbacks)))
        # Version 4 places the ID in the high 16 bits of lParam.
        identity = next(_IDS) % 0xFFFF or 1
        while identity in self._callbacks:
            identity = next(_IDS) % 0xFFFF or 1
        value = self._data(identity)
        value.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        value.uCallbackMessage, value.hIcon = CALLBACK_MESSAGE, self._icon
        value.szTip = "灵波 · 定时提醒"
        if not self._shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(value)):
            return False
        self._callbacks[identity] = on_click
        value.uTimeoutOrVersion = 4
        if not self._shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(value)):
            self._remove(identity)
            return False
        value.uFlags = NIF_INFO
        value.szInfoTitle, value.szInfo = _utf16_prefix(title, 64), _utf16_prefix(body, 256)
        value.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND | NIIF_RESPECT_QUIET_TIME
        value.uTimeoutOrVersion = 10000
        if not self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(value)):
            self._remove(identity)
            return False
        # Bounded lifetime even if Explorer/DND suppresses all callbacks.
        timer = QTimer(self._window)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._remove(identity))
        self._timers[identity] = timer
        timer.start(60000)
        return True  # Accepted by Shell, not a guarantee Windows displayed it.

    def _remove(self, identity: int) -> None:
        if identity in self._callbacks:
            self._callbacks.pop(identity)
            value = self._data(identity)
            self._shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(value))
        timer = self._timers.pop(identity, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int,
    ) -> tuple[bool, int]:
        if not self._callbacks:
            return False, 0
        kind = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if kind not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        native = wintypes.MSG.from_address(int(message))
        if native.hWnd != self._hwnd or native.message != CALLBACK_MESSAGE:
            return False, 0
        identity = (int(native.lParam) >> 16) & 0xFFFF
        event = int(native.lParam) & 0xFFFF
        callback = self._callbacks.get(identity)
        if callback is not None:
            if event == NIN_BALLOONUSERCLICK:
                self._remove(identity)
                QTimer.singleShot(0, self._window, callback)
            elif event in (NIN_BALLOONHIDE, NIN_BALLOONTIMEOUT):
                self._remove(identity)
        return True, 0

    def close(self) -> None:
        for identity in list(self._callbacks):
            self._remove(identity)
        app = QApplication.instance()
        if self._installed and app is not None:
            app.removeNativeEventFilter(self)
        self._installed = False
        if self._icon and self._user is not None:
            self._user.DestroyIcon(self._icon)
        self._icon = 0
