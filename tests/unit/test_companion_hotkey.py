from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest
from PySide6.QtCore import QByteArray

from limbowave.ui.companion_hotkey import CompanionHotkey, hotkey_parts


@pytest.mark.parametrize("sequence, expected", [
    ("Ctrl+Alt+Space", (0x4003, 32)), ("Alt+M", (0x4001, ord("M"))),
    ("Ctrl+Shift+F11", (0x4006, 0x7A)), ("Ctrl+7", (0x4002, ord("7"))),
    ("M", None), ("Shift+M", None), ("Ctrl+F12", None),
    ("Meta+M", None), ("Ctrl+K, Ctrl+M", None), ("", None),
])
def test_hotkey_validation(sequence, expected):
    assert hotkey_parts(sequence) == expected


class FakeApi:
    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.accept = True

    def RegisterHotKey(self, hwnd, key, modifiers, virtual):
        self.registered.append(key)
        return self.accept

    def UnregisterHotKey(self, hwnd, key):
        self.unregistered.append(key)
        return True


def test_conflict_preserves_binding_and_cleanup_is_idempotent(qapp):
    hotkey = CompanionHotkey(lambda: None)
    hotkey._api = api = FakeApi()
    assert hotkey.register("Ctrl+Alt+Space")[0]
    first = hotkey._id
    api.accept = False
    success, message = hotkey.register("Ctrl+Alt+M")
    assert not success and "占用" in message
    assert hotkey._id == first
    assert api.unregistered == []
    api.accept = True
    assert hotkey.register("Ctrl+Alt+M")[0]
    second = hotkey._id
    assert api.unregistered == [first]
    hotkey.close()
    hotkey.close()
    assert api.unregistered == [first, second]


def test_native_filter_handles_only_registered_id(qapp):
    called = []
    hotkey = CompanionHotkey(lambda: called.append(True))
    hotkey._api = FakeApi()
    hotkey.register("Ctrl+Alt+Space")
    message = wintypes.MSG()
    message.message = 0x0312
    message.wParam = hotkey._id
    assert hotkey.nativeEventFilter(QByteArray(b"windows_dispatcher_MSG"),
                                   ctypes.addressof(message)) == (True, 0)
    assert called == [True]
    message.wParam += 1
    assert not hotkey.nativeEventFilter(b"windows_dispatcher_MSG",
                                       ctypes.addressof(message))[0]
    hotkey.close()


def test_unavailable_platform_has_a_visible_fallback(qapp):
    hotkey = CompanionHotkey(lambda: None)
    hotkey._api = None
    success, message = hotkey.register("Ctrl+Alt+Space")
    assert not success and "Ctrl+Shift+M" in message
    hotkey.close()
