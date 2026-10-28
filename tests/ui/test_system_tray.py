from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
from pytestqt.qtbot import QtBot

from limbowave.bootstrap import APP_DISPLAY_NAME
from limbowave.ui.app_icon import APP_ICON_PATH, application_icon
from limbowave.ui.main_window import MainWindow
from limbowave.ui.system_tray import SystemTray


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48, 256])
def test_bundled_icon_renders_at_runtime_sizes(qapp: QApplication, size: int) -> None:
    assert APP_ICON_PATH.is_file()
    icon = application_icon()
    assert not icon.isNull()
    image = icon.pixmap(size, size).toImage()
    assert not image.isNull()
    assert any(image.pixelColor(x, y).alpha() for x in range(size) for y in range(size))


def test_existing_application_uses_bundled_icon(qapp: QApplication) -> None:
    from limbowave.app import build_application

    original = qapp.windowIcon()
    try:
        assert build_application([]) is qapp
        assert qapp.windowIcon().pixmap(32, 32).toImage() == (
            application_icon().pixmap(32, 32).toImage()
        )
    finally:
        qapp.setWindowIcon(original)


def test_tray_uses_bundled_icon_and_window_ownership(qtbot: QtBot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    tray = SystemTray(window)
    assert tray.parent() is window
    assert tray.toolTip() == APP_DISPLAY_NAME
    assert tray.icon().pixmap(32, 32).toImage() == application_icon().pixmap(32, 32).toImage()
    menu = tray.contextMenu()
    assert menu is not None
    assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
        "显示主窗口", "退出"
    ]


@pytest.mark.parametrize("reason", [
    QSystemTrayIcon.ActivationReason.Trigger,
    QSystemTrayIcon.ActivationReason.DoubleClick,
])
def test_tray_activation_restores_minimized_window(qtbot: QtBot, reason) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    tray = SystemTray(window)
    window.setWindowState(Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized)
    tray.activated.emit(reason)
    assert window.isVisible()
    assert not window.isMinimized()
    assert window.isMaximized()


def test_context_activation_does_not_show_window(qtbot: QtBot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    tray = SystemTray(window)
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Context)
    assert not window.isVisible()


def test_menu_shows_window(qtbot: QtBot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    tray = SystemTray(window)
    menu = tray.contextMenu()
    assert menu is not None
    menu.actions()[0].trigger()
    assert window.isVisible()


def test_tray_exit_respects_main_window_close_guard(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    tray = SystemTray(window)
    window.show()
    window.set_close_guard(lambda: False)
    menu = tray.contextMenu()
    assert menu is not None
    menu.actions()[-1].trigger()
    assert window.isVisible()
    window.set_close_guard(lambda: True)
    menu.actions()[-1].trigger()
    assert not window.isVisible()
