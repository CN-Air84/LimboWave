"""Paint-only entrance effect for newly sent messages; never moves layout items."""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGraphicsEffect

_SEND_OFFSET = 14.0


class MessageSendEffect(QGraphicsEffect):
    """Fade and lift the whole row, including attachments, in logical pixels."""

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._progress = 0.0

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, value))
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def boundingRectFor(self, sourceRect: QRectF | QRect) -> QRectF:
        # Reserve painting room below the resting row, not extra layout space.
        return QRectF(sourceRect).adjusted(0.0, 0.0, 0.0, _SEND_OFFSET)

    def draw(self, painter: QPainter) -> None:
        if self._progress <= 0.0:
            return
        offset = QPoint()
        pixmap = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates,
            offset,
            QGraphicsEffect.PixmapPadMode.NoPad,
        )
        painter.save()
        painter.setOpacity(painter.opacity() * self._progress)
        painter.drawPixmap(
            QPointF(offset) + QPointF(0.0, _SEND_OFFSET * (1.0 - self._progress)), pixmap
        )
        painter.restore()
