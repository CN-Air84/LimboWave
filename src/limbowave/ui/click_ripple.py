"""Subtle click ripples clipped to one control (or one item-view row)."""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QRadialGradient
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QTextEdit,
    QWidget,
)
from shiboken6 import isValid

from limbowave.ui import theme


class _RippleLayer(QWidget):
    """One independent, bounded ripple; removed only when it finishes or is cancelled."""

    def __init__(self, target: QWidget) -> None:
        super().__init__(target)
        self.setObjectName("clickRippleLayer")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent; border: none;")
        self.origin = QPointF()
        self.progress = 0.0
        self.radius = 0.0
        self.corner_radius = float(theme.RADIUS_MD)
        self.color = QColor("#E8E8E8")
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(1200)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self._dispose)
        target.installEventFilter(self)
        self.hide()

    def start(self, bounds: QRect, position: QPointF, *, row: bool = False) -> None:
        self.animation.stop()
        self.setGeometry(bounds)
        self.origin = position - QPointF(bounds.topLeft())
        self.corner_radius = 0.0 if row else float(theme.RADIUS_MD)
        self.radius = max(
            math.hypot(self.origin.x() - x, self.origin.y() - y)
            for x in (0, bounds.width())
            for y in (0, bounds.height())
        )
        self.progress = 0.0
        self.show()
        self.raise_()
        self.animation.start()

    def _step(self, value: object) -> None:
        self.progress = float(value)  # type: ignore[arg-type]
        self.update()

    def _dispose(self) -> None:
        self.animation.stop()
        self.hide()
        self.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Hide, QEvent.Type.Wheel):
            self._dispose()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), self.corner_radius, self.corner_radius)
        painter.setClipPath(clip)
        radius = max(1.0, self.radius * self.progress)
        gradient = QRadialGradient(self.origin, radius)
        # Steady expansion, with a short visible lead-in and a smooth fade to zero.
        fade = max(0.0, min(1.0, (self.progress - 0.15) / 0.85))
        opacity = 1.0 - fade * fade * (3.0 - 2.0 * fade)
        for stop, alpha in ((0.0, 0.025), (0.65, 0.055), (0.84, 0.12), (1.0, 0.0)):
            color = QColor(self.color)
            color.setAlphaF(alpha * opacity)
            gradient.setColorAt(stop, color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(self.origin, radius, radius)
        painter.end()


class _ClickRipples(QObject):
    def __init__(self, root: QWidget) -> None:
        super().__init__(root)
        self.root = root

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isValid(self) or not isValid(self.root):
            return False
        if not isValid(watched):
            return True
        if (
            event.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
            or not isinstance(event, QMouseEvent)
            or event.button() != Qt.MouseButton.LeftButton
            or not isinstance(watched, QWidget)
            or not watched.isEnabled()
            or not (watched is self.root or self.root.isAncestorOf(watched))
        ):
            return False
        target = watched
        parent = target.parentWidget()
        row = False
        bounds = target.rect()
        if isinstance(parent, QAbstractItemView) and target is parent.viewport():
            index = parent.indexAt(event.position().toPoint())
            if not index.isValid() or not (index.flags() & Qt.ItemFlag.ItemIsEnabled):
                return False
            bounds = parent.visualRect(index)
            row = True
        else:
            if isinstance(parent, (QComboBox, QAbstractSpinBox, QPlainTextEdit, QTextEdit)):
                target = parent
            if not isinstance(target, (
                QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit,
            )) or isinstance(target, (QCheckBox, QRadioButton)):
                return False
            bounds = target.rect()
        if target.property("themeEffectDisabled") or target.property("rippleDisabled"):
            return False
        # Each click owns its overlay and timeline, including clicks on different rows.
        layer = _RippleLayer(target)
        if row and isinstance(parent, QAbstractItemView):
            parent.verticalScrollBar().valueChanged.connect(layer._dispose)
            parent.horizontalScrollBar().valueChanged.connect(layer._dispose)
        position = QPointF(target.mapFromGlobal(event.globalPosition().toPoint()))
        layer.start(bounds, position, row=row)
        return False


def install_click_ripples(root: QWidget) -> None:
    """Install once, including controls created later, without changing their styles."""
    if isinstance(getattr(root, "_limbowave_click_ripples", None), _ClickRipples):
        return
    app = QApplication.instance()
    if app is not None:
        controller = _ClickRipples(root)
        root._limbowave_click_ripples = controller  # type: ignore[attr-defined]
        app.installEventFilter(controller)
