"""Qt 日志桥接与独立诊断浮窗；Qt 依赖只留在 UI 层。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import (
    QEvent,
    QMessageLogContext,
    QObject,
    Qt,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from limbowave.infrastructure.diagnostics import LogLevel, LogManager
from limbowave.infrastructure.diagnostics.runtime import emergency_notice
from limbowave.ui.diagnostics_panel import DiagnosticsPanel

QtHandler = Callable[[QtMsgType, QMessageLogContext, str], None]
_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


class QtDiagnosticsBridge:
    def __init__(self, manager: LogManager) -> None:
        self._manager = manager
        self._log = manager.get_logger("limbowave.qt")
        self._handler: QtHandler = self._handle
        self._previous: QtHandler | None = None
        self._installed = False

    def __enter__(self) -> QtDiagnosticsBridge:
        if not self._installed:
            self._previous = qInstallMessageHandler(self._handler)
            self._installed = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._installed:
            current = qInstallMessageHandler(self._previous)
            if current is not self._handler:
                # 安装期间若另有接管者，保留它；Qt 没有无副作用的 handler getter。
                qInstallMessageHandler(current)
            self._installed = False

    def _handle(self, kind: QtMsgType, context: QMessageLogContext, message: str) -> None:
        try:
            self._log.log(
                _QT_LEVELS.get(kind, logging.WARNING),
                "%s",
                message,
                extra={
                    "qt_category": context.category,
                    "qt_file": context.file,
                    "qt_line": context.line,
                    "qt_function": context.function,
                },
            )
            if kind == QtMsgType.QtFatalMsg:
                self._manager.flush(timeout=0.25)
                emergency_notice(message)
            if self._previous is not None:
                self._previous(kind, context, message)
        except Exception:
            emergency_notice("Qt 日志处理失败。")


class DiagnosticsWindow(QDialog):
    """工具窗不维持应用存活；面板首次打开时才创建，重复打开复用。"""

    def __init__(
        self,
        manager: LogManager,
        parent: QWidget,
        *,
        on_level_changed: Callable[[LogLevel], None] | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setObjectName("diagnosticsWindow")
        self.setWindowTitle("诊断日志 · Ctrl+Shift+L")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.resize(1050, 650)
        self._manager = manager
        self._on_level_changed = on_level_changed
        self._panel: DiagnosticsPanel | None = None
        self._layout = QVBoxLayout(self)
        parent.installEventFilter(self)

    def show_diagnostics(self) -> None:
        if self._panel is None:
            self._panel = DiagnosticsPanel(
                self._manager, self, on_level_changed=self._on_level_changed
            )
            self._layout.addWidget(self._panel)
        self.show()
        self.raise_()
        self.activateWindow()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Close:
            self.close()
        return super().eventFilter(watched, event)
