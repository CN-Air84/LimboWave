"""Optional Windows global shortcut. No hooks, threads or background polling."""
from __future__ import annotations

import ctypes
import itertools
import sys
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication

_IDS = itertools.count(0x5100)


def hotkey_parts(sequence: str) -> tuple[int, int] | None:
    """Only modified single strokes with unambiguous Win32 virtual keys."""
    parsed = QKeySequence(sequence)
    if parsed.count() != 1:
        return None
    from PySide6.QtCore import Qt
    combination = parsed[0]  # type: ignore[index]  # Qt binding omits __getitem__ stub.
    qt_mods = combination.keyboardModifiers()
    if qt_mods & Qt.KeyboardModifier.MetaModifier:
        return None  # Windows-logo combinations are reserved by the OS.
    modifiers = 0
    for qt_mod, native in ((Qt.KeyboardModifier.ControlModifier, 2),
                           (Qt.KeyboardModifier.AltModifier, 1),
                           (Qt.KeyboardModifier.ShiftModifier, 4)):
        if qt_mods & qt_mod:
            modifiers |= native
    if not modifiers & 3:
        return None
    key = int(combination.key())
    if ord("A") <= key <= ord("Z") or ord("0") <= key <= ord("9") or key == 32:
        return modifiers | 0x4000, key  # MOD_NOREPEAT
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F11:
        return modifiers | 0x4000, 0x70 + key - int(Qt.Key.Key_F1)
    return None  # F12 is debugger-reserved; arbitrary punctuation is layout-dependent.


class CompanionHotkey(QAbstractNativeEventFilter):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback
        self._id: int | None = None
        self._api: Any = None
        self._installed = False
        self.sequence = ""
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            self._api = ctypes.WinDLL("user32", use_last_error=True)
            self._api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                                wintypes.UINT, wintypes.UINT]
            self._api.RegisterHotKey.restype = wintypes.BOOL
            self._api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._api.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, sequence: str) -> tuple[bool, str]:
        if self._id is not None and sequence == self.sequence:
            return True, f"全局呼出：{sequence}"
        parts = hotkey_parts(sequence)
        if parts is None:
            return False, "请选择 Ctrl/Alt 加字母、数字、空格或 F1–F11 的单个组合键。"
        if self._api is None:
            return False, "当前平台不支持全局呼出；请使用主界面按钮或应用内 Ctrl+Shift+M。"
        candidate = next(_IDS)
        if not self._api.RegisterHotKey(None, candidate, *parts):
            return False, "快捷键被占用或注册失败；可重新设置，或使用应用内 Ctrl+Shift+M。"
        self.close()
        self._id = candidate
        self.sequence = sequence
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
            self._installed = True
        return True, f"全局呼出：{sequence} · 再按一次收起"

    def close(self) -> None:
        if self._api is not None and self._id is not None:
            self._api.UnregisterHotKey(None, self._id)
        self._id = None
        app = QApplication.instance()
        if self._installed and app is not None:
            app.removeNativeEventFilter(self)
        self._installed = False

    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int,
    ) -> tuple[bool, int]:
        kind = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if (self._id is not None and kind in
                (b"windows_generic_MSG", b"windows_dispatcher_MSG")):
            native = wintypes.MSG.from_address(int(message))
            if native.message == 0x0312 and native.wParam == self._id:
                self._callback()
                return True, 0
        return False, 0
