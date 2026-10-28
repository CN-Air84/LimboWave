"""Runtime tray entry; closing the window still follows the normal exit guards."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from limbowave.bootstrap import APP_DISPLAY_NAME
from limbowave.ui.app_icon import application_icon


class SystemTray(QSystemTrayIcon):
    def __init__(self, window: QWidget) -> None:
        super().__init__(application_icon(), window)
        self._window = window
        self.setToolTip(APP_DISPLAY_NAME)
        self._menu = QMenu(window)
        show_action = QAction("显示主窗口", self._menu)
        self._menu.addAction(show_action)
        show_action.triggered.connect(self._show_window)
        self._menu.addSeparator()
        quit_action = QAction("退出", self._menu)
        self._menu.addAction(quit_action)
        # Do not call QApplication.quit(): the window owns unsaved-change guards.
        quit_action.triggered.connect(window.close)
        self.setContextMenu(self._menu)
        self.activated.connect(self._activate)

    def _show_window(self) -> None:
        # Removing only the minimized flag preserves a maximized window's state.
        self._window.setWindowState(
            self._window.windowState() & ~Qt.WindowState.WindowMinimized
        )
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _activate(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()
