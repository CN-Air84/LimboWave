"""Win10-style Reveal lighting, clipped to the hovered control or item-view row."""

from __future__ import annotations

import weakref

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QMargins,
    QObject,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QHoverEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
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

_RADIUS = 96.0  # Logical pixels; Qt handles high-DPI scaling.
_CONTROLS = (QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit)
_HOVER_EVENTS = (QEvent.Type.HoverEnter, QEvent.Type.HoverMove, QEvent.Type.HoverLeave)
_CLEAR_EVENTS = (
    QEvent.Type.Hide,
    QEvent.Type.Move,
    QEvent.Type.Resize,
    QEvent.Type.Wheel,
    QEvent.Type.EnabledChange,
)


def _surface(widget: QWidget) -> QWidget | None:
    """Normalize Qt's internal editor/viewport without lighting passive containers."""
    if not isValid(widget):
        return None
    parent = widget.parentWidget()
    if isinstance(parent, QAbstractItemView) and widget is parent.viewport():
        return widget
    if isinstance(parent, (QComboBox, QAbstractSpinBox)) and isinstance(widget, QLineEdit):
        return parent
    if isinstance(parent, (QPlainTextEdit, QTextEdit)) and widget is parent.viewport():
        return parent
    if isinstance(widget, _CONTROLS) and not isinstance(widget, (QCheckBox, QRadioButton)):
        return widget
    return None


class _RevealLayer(QWidget):
    """A small transparent patch; never an effect on the control's text or background."""

    def __init__(self, target: QWidget) -> None:
        super().__init__(target)
        self.setObjectName("cursorRevealLayer")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent; border: none;")
        self.origin = QPointF()
        self.surface_rect = QRectF()
        self.corner_radius = float(theme.RADIUS_MD)
        self.opacity = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self._finished)
        parent = target.parentWidget()
        scroll_area = parent if isinstance(parent, QAbstractItemView) else target
        if isinstance(scroll_area, (QAbstractItemView, QPlainTextEdit, QTextEdit)):
            scroll_area.verticalScrollBar().valueChanged.connect(self.cancel)
            scroll_area.horizontalScrollBar().valueChanged.connect(self.cancel)
        if isinstance(scroll_area, QAbstractItemView):
            model = scroll_area.model()
            if model is not None:
                model.modelReset.connect(self.cancel)
                model.rowsAboutToBeRemoved.connect(self.cancel)
                model.rowsAboutToBeInserted.connect(self.cancel)
                model.layoutAboutToBeChanged.connect(self.cancel)
                model.dataChanged.connect(self.cancel)
        self.hide()

    def reveal(self, bounds: QRect, position: QPointF, *, corner_radius: float) -> None:
        patch = QRectF(
            position.x() - _RADIUS, position.y() - _RADIUS, _RADIUS * 2, _RADIUS * 2
        ).toAlignedRect().intersected(bounds)
        self.setGeometry(patch)
        self.origin = position - QPointF(patch.topLeft())
        self.surface_rect = QRectF(bounds.translated(-patch.topLeft()))
        self.corner_radius = corner_radius
        self.show()
        self.raise_()
        self._fade(1.0, 120)
        self.update()

    def dismiss(self) -> None:
        self._fade(0.0, 180)

    def cancel(self) -> None:
        self.animation.stop()
        self.opacity = 0.0
        self.hide()

    def _fade(self, opacity: float, duration: int) -> None:
        if (
            self.animation.state() == QVariantAnimation.State.Running
            and self.animation.endValue() == opacity
        ):
            return
        self.animation.stop()
        if not QApplication.isEffectEnabled(Qt.UIEffect.UI_General):
            self.opacity = opacity
            self.update()
            self._finished()
        elif self.opacity != opacity:
            start = self.opacity
            self.animation.setDuration(duration)
            self.animation.setStartValue(start)
            self.animation.setEndValue(opacity)
            self.animation.start()
        else:
            self._finished()

    def _step(self, value: object) -> None:
        self.opacity = float(value)  # type: ignore[arg-type]
        self.update()

    def _finished(self) -> None:
        if self.opacity <= 0.0:
            self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.surface_rect, self.corner_radius, self.corner_radius)
        painter.setClipPath(path)
        dark = QColor(theme.BG_SURFACE).lightnessF() < 0.5
        base = QColor("#ffffff" if dark else theme.ACCENT)
        gradient = QRadialGradient(self.origin, _RADIUS)
        strength = 0.12 if dark else 0.09
        for stop, falloff in ((0.0, 1.0), (0.35, 0.65), (0.7, 0.2), (1.0, 0.0)):
            color = QColor(base)
            color.setAlphaF(strength * falloff * self.opacity)
            gradient.setColorAt(stop, color)
        painter.fillPath(path, gradient)
        border = QRadialGradient(self.origin, _RADIUS)
        color = QColor(base)
        color.setAlphaF((0.42 if dark else 0.26) * self.opacity)
        border.setColorAt(0.0, color)
        color.setAlphaF(0.0)
        border.setColorAt(1.0, color)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            self.surface_rect.adjusted(0.5, 0.5, -0.5, -0.5),
            self.corner_radius,
            self.corner_radius,
        )
        painter.end()


class _CursorReveal(QObject):
    def __init__(self, root: QWidget) -> None:
        super().__init__(root)
        self._root_ref = weakref.ref(root)
        self._active: _RevealLayer | None = None
        self._pending_prepare: dict[int, weakref.ReferenceType[QWidget]] = {}
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setSingleShot(True)
        self._prepare_timer.timeout.connect(self._prepare_pending)
        for widget in (root, *root.findChildren(QWidget)):
            self._prepare(widget)

    def _owns(self, widget: QWidget) -> bool:
        root = self._root_ref()
        return root is not None and isValid(root) and isValid(widget) and (
            widget is root or root.isAncestorOf(widget)
        )

    def _prepare(self, widget: QWidget) -> None:
        if not isValid(widget):
            return
        surface = _surface(widget)
        # Resolving a lazily-created viewport can replace the old child.
        if not isValid(widget):
            return
        if surface is not None:
            widget.setAttribute(Qt.WidgetAttribute.WA_Hover)
        if isinstance(widget, (QAbstractItemView, QPlainTextEdit, QTextEdit)):
            viewport = widget.viewport()
            if isValid(viewport):
                viewport.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def _queue_prepare(self, widget: QWidget) -> None:
        # Polish/Show may occur inside QTreeWidget construction. Calling viewport()
        # there re-enters construction and can destroy the receiver of this event.
        # Weak references also avoid retaining removed pages or the window itself.
        self._pending_prepare[id(widget)] = weakref.ref(widget)
        if not self._prepare_timer.isActive():
            self._prepare_timer.start(0)

    def _prepare_pending(self) -> None:
        pending, self._pending_prepare = self._pending_prepare, {}
        for reference in pending.values():
            widget = reference()
            if widget is not None and self._owns(widget):
                self._prepare(widget)

    def _live_layer(self) -> _RevealLayer | None:
        if getattr(self, "_active", None) is not None and not isValid(self._active):
            self._active = None
        return getattr(self, "_active", None)

    def _clear(self, *, immediate: bool = False) -> None:
        layer = self._live_layer()
        self._active = None
        if layer is not None:
            if immediate:
                layer.cancel()
            else:
                layer.dismiss()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Qt can deliver teardown events while Python wrappers are being collected.
        if not hasattr(self, "_root_ref") or not isValid(self):
            return False
        if not isValid(watched):
            return True
        kind = event.type()
        if kind == QEvent.Type.ApplicationDeactivate:
            self._clear(immediate=True)
            return False
        if not isinstance(watched, QWidget):
            return False
        if kind in (QEvent.Type.Polish, QEvent.Type.Show):
            if self._owns(watched):
                self._queue_prepare(watched)
            return False
        if kind in (
            *_CLEAR_EVENTS, QEvent.Type.WindowDeactivate, QEvent.Type.DynamicPropertyChange
        ):
            layer = self._live_layer()
            target = layer.parentWidget() if layer is not None else None
            if target is not None and (watched is target or watched.isAncestorOf(target)):
                self._clear(immediate=True)
            return False
        if kind not in _HOVER_EVENTS or not self._owns(watched):
            return False
        target = _surface(watched)
        if target is None:
            return False
        if kind == QEvent.Type.HoverLeave:
            layer = self._live_layer()
            if layer is not None and layer.parentWidget() is target:
                self._clear()
            return False
        if not isinstance(event, QHoverEvent):
            return False
        if (
            not target.isEnabled()
            or target.property("themeEffectDisabled")
            or target.property("revealDisabled")
        ):
            self._clear()
            return False
        position = target.mapFromGlobal(watched.mapToGlobal(event.position()))
        bounds = target.rect()
        corner_radius = float(theme.RADIUS_MD)
        parent = target.parentWidget()
        row = isinstance(parent, QAbstractItemView) and target is parent.viewport()
        if row and isinstance(parent, QAbstractItemView):
            index = parent.indexAt(position.toPoint())
            if not index.isValid() or not (index.flags() & Qt.ItemFlag.ItemIsEnabled):
                self._clear()
                return False
            if parent.property("themeEffectDisabled") or parent.property("revealDisabled"):
                self._clear()
                return False
            # Row effects default to square; rounded navigation lists opt in.
            corner_radius = float(parent.property("revealItemRadius") or 0.0)
            bounds = parent.visualRect(index)
            margins = parent.property("revealItemMargins")
            if isinstance(margins, QMargins):
                bounds = bounds.marginsRemoved(margins)
            bounds = bounds.intersected(target.rect())
        if not bounds.contains(position.toPoint()):
            self._clear()
            return False
        layer = self._live_layer()
        if layer is None or layer.parentWidget() is not target:
            self._clear()
            cached = getattr(target, "_limbowave_reveal_layer", None)
            layer = cached if isinstance(cached, _RevealLayer) and isValid(cached) else None
            if layer is None:
                layer = _RevealLayer(target)
                target._limbowave_reveal_layer = layer  # type: ignore[attr-defined]
            self._active = layer
        layer.reveal(bounds, position, corner_radius=corner_radius)
        return False


def install_cursor_reveal(root: QWidget) -> None:
    """Install once; defer discovery until new controls finish construction."""
    controller = getattr(root, "_limbowave_cursor_reveal", None)
    if isinstance(controller, _CursorReveal) and isValid(controller):
        layer = controller._live_layer()
        if layer is not None:
            layer.update()
        return
    app = QApplication.instance()
    if app is not None:
        controller = _CursorReveal(root)
        root._limbowave_cursor_reveal = controller  # type: ignore[attr-defined]
        app.installEventFilter(controller)
