"""Static no-wallpaper glow, shared by the chat and its scrolling transcript.

This is a paint-only fallback: no wallpaper assets, background jobs or animation.
Coordinates belong to the viewport, so scrolling never moves the ambient light.
"""

from PySide6.QtCore import QPoint, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget

from limbowave.ui import theme


def paint_ambient_background(
    widget: QWidget, painter: QPainter, *, canvas: QWidget | None = None
) -> None:
    if not theme.ambient_background_active():
        return
    canvas = canvas or widget
    origin = canvas.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
    width, height = canvas.width(), canvas.height()
    extent = max(width, height, 1)
    painter.save()
    painter.setClipRect(widget.rect())
    painter.fillRect(widget.rect(), QColor(theme.BG_APP))
    painter.translate(-origin.x(), -origin.y())
    for x, y, radius, color in (
        (0.08, 0.08, 0.85, QColor(88, 101, 242, 20)),
        (0.92, 0.85, 0.65, QColor(115, 80, 180, 10)),
    ):
        gradient = QRadialGradient(QPointF(width * x, height * y), extent * radius)
        gradient.setColorAt(0.0, color)
        transparent = QColor(color)
        transparent.setAlpha(0)
        gradient.setColorAt(1.0, transparent)
        painter.fillRect(QRectF(canvas.rect()), gradient)
    painter.restore()
