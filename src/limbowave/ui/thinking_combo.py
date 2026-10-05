"""Unavailable thinking levels require a deliberate three-second hold."""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget

from limbowave.ui.popup_motion import UpwardComboBox

HOLD_MS = 3000
HOLD_PROGRESS_ROLE = int(Qt.ItemDataRole.UserRole) + 31


class ThinkingComboBox(UpwardComboBox):
    force_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._held_row = -1
        self._elapsed = QElapsedTimer()
        self._hold_timer = QTimer(self)
        self._hold_timer.setInterval(30)
        self._hold_timer.timeout.connect(self._tick_hold)
        self.view().viewport().installEventFilter(self)
        self.view().window().installEventFilter(self)

    def cancel_hold(self) -> None:
        row, self._held_row = self._held_row, -1
        self._hold_timer.stop()
        if row >= 0:
            self.model().setData(self.model().index(row, 0), None, HOLD_PROGRESS_ROLE)

    def _tick_hold(self) -> None:
        if self._held_row < 0 or not self.isEnabled() or not self.view().isVisible():
            self.cancel_hold()
            return
        index = self.model().index(self._held_row, 0)
        if index.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.cancel_hold()
            return
        progress = min(1.0, self._elapsed.elapsed() / HOLD_MS)
        self.model().setData(index, progress, HOLD_PROGRESS_ROLE)
        if progress >= 1.0:
            level = self.itemText(self._held_row)
            self.cancel_hold()
            self.hidePopup()
            self.force_requested.emit(level)

    def hidePopup(self) -> None:
        self.cancel_hold()
        super().hidePopup()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.Leave, QEvent.Type.WindowDeactivate):
            self.cancel_hold()
        if watched is self.view().viewport() and isinstance(event, QMouseEvent):
            row = self.view().indexAt(event.position().toPoint()).row()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.cancel_hold()
                if row >= 0 and not self.model().index(row, 0).flags() & Qt.ItemFlag.ItemIsEnabled:
                    self._held_row = row
                    self._elapsed.start()
                    self._hold_timer.start()
                    return True
            elif kind == QEvent.Type.MouseMove and self._held_row >= 0:
                if row != self._held_row or not event.buttons() & Qt.MouseButton.LeftButton:
                    self.cancel_hold()
                return True
            elif kind == QEvent.Type.MouseButtonRelease and self._held_row >= 0:
                self.cancel_hold()
                return True
        return super().eventFilter(watched, event)
