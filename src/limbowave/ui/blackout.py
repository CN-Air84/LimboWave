"""A stationary black veil with bounded-clock opacity fades."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QElapsedTimer, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QHideEvent, QKeyEvent, QPainter, QPaintEvent, QShowEvent
from PySide6.QtWidgets import QWidget


class Blackout(QWidget):
    """Fade a translucent child, without snapshots or moving the underlying page."""

    finished = Signal()
    FADE_IN_MS = 240
    FADE_OUT_MS = 320

    def __init__(self, parent: QWidget, *, opacity: float = 0.0) -> None:
        super().__init__(parent)
        self.setObjectName("loginBlackout")
        self.setStyleSheet("background: transparent;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)
        self._opacity = opacity
        self._from = opacity
        self._target = opacity
        self._elapsed_ms = 0
        self._duration = 1
        self._active = False
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance)
        self._easing = QEasingCurve(QEasingCurve.Type.InOutSine)
        self.hide()

    @property
    def opacity(self) -> float:
        return self._opacity

    def fade_to(self, opacity: float, duration: int) -> None:
        self._from = self._opacity
        self._target = opacity
        self._duration = duration
        self._elapsed_ms = 0
        self._active = True
        self.show()
        self.raise_()
        self.setFocus()
        self._clock.start()
        self._timer.start()

    def dismiss(self) -> None:
        """Cancel a failed submission immediately, without a fade or finished signal."""
        self._timer.stop()
        self._active = False
        self._opacity = 0.0
        self._from = 0.0
        self._target = 0.0
        self._elapsed_ms = 0
        self.hide()
        self.update()

    def _advance(self) -> None:
        if not self._active:
            return
        self._elapsed_ms += min(32, self._clock.restart())
        fraction = min(1.0, self._elapsed_ms / self._duration)
        value = self._easing.valueForProgress(fraction)
        self._opacity = self._from + (self._target - self._from) * value
        self.update()
        if fraction >= 1.0:
            self._timer.stop()
            self._active = False
            self.finished.emit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            parent = self.parentWidget()
            if parent is not None:
                self.setGeometry(parent.rect())
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._active:
            self._clock.start()
            self._timer.start()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        color = QColor(Qt.GlobalColor.black)
        color.setAlphaF(self._opacity)
        painter.fillRect(self.rect(), color)
