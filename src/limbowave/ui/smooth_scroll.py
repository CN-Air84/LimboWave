"""应用级鼠标滚轮缓动与中键自动滚动。

只接管传统滚轮的 ``angleDelta``；触控板已经提供像素级连续输入，继续交给 Qt，
避免在原生惯性之上再叠一层动画。嵌套滚动区到达边界后会自然交给外层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import copysign, trunc

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QScrollBar,
    QWidget,
)
from shiboken6 import isValid

from limbowave.ui import theme

_FILTER_NAME = "limbowaveSmoothScroll"


@dataclass(slots=True)
class _Motion:
    animation: QVariantAnimation
    target: int
    direction: int


class _AutoScrollMarker(QWidget):
    """固定在视口上的中键原点，不截获鼠标或键盘输入。"""

    def __init__(self, parent: QWidget, *, horizontal: bool, vertical: bool) -> None:
        super().__init__(parent)
        self.setObjectName("middleScrollOrigin")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent; border: none;")
        self.setFixedSize(29, 29)
        self._horizontal = horizontal
        self._vertical = vertical

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QColor(theme.BG_ELEVATED)
        background.setAlpha(240)
        foreground = QColor(theme.TEXT_PRIMARY)
        painter.setBrush(background)
        painter.setPen(QPen(QColor(theme.BORDER), 1.0))
        painter.drawEllipse(QRectF(1, 1, 27, 27))
        painter.setBrush(foreground)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(14, 14), 1.5, 1.5)
        if self._vertical:
            painter.drawPolygon(QPolygonF([QPointF(14, 4), QPointF(10, 9), QPointF(18, 9)]))
            painter.drawPolygon(QPolygonF([QPointF(14, 24), QPointF(10, 19), QPointF(18, 19)]))
        if self._horizontal:
            painter.drawPolygon(QPolygonF([QPointF(4, 14), QPointF(9, 10), QPointF(9, 18)]))
            painter.drawPolygon(QPolygonF([QPointF(24, 14), QPointF(19, 10), QPointF(19, 18)]))


@dataclass(slots=True)
class _AutoAxis:
    area: QAbstractScrollArea
    bar: QScrollBar
    horizontal: bool
    remainder: float = 0.0


@dataclass(slots=True)
class _AutoScroll:
    axes: list[_AutoAxis]
    origin: QPoint
    marker: _AutoScrollMarker
    connections: list[QMetaObject.Connection] = field(default_factory=list)


class SmoothScrollFilter(QObject):
    """统一处理滚轮缓动和所有 ``QAbstractScrollArea`` 的中键自动滚动。"""

    DURATION_MS = 180
    AUTO_INTERVAL_MS = 16
    AUTO_DEAD_ZONE = 12
    AUTO_MAX_SPEED = 1800.0

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._motions: dict[QScrollBar, _Motion] = {}
        self._auto_scroll: _AutoScroll | None = None
        self._swallowed_button = Qt.MouseButton.NoButton
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(self.AUTO_INTERVAL_MS)
        self._auto_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._auto_timer.timeout.connect(self._auto_tick)
        self._auto_clock = QElapsedTimer()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isValid(self):
            return False
        if not isValid(watched):
            return True
        kind = event.type()
        if self._auto_event(watched, event) or not isValid(watched):
            return True
        if kind == QEvent.Type.MouseButtonPress and isinstance(watched, QScrollBar):
            self._cancel(watched)
            return False
        if kind != QEvent.Type.Wheel or not isinstance(event, QWheelEvent):
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False

        horizontal, delta = self._wheel_axis(event)
        candidates = self._scroll_areas(watched)
        if event.pixelDelta().x() or event.pixelDelta().y():
            for area in candidates:
                self._cancel(self._bar(area, horizontal))
            return False
        if delta == 0:
            return False

        direction = -1 if delta > 0 else 1
        for area in candidates:
            if area.property("smoothScrollDisabled"):
                continue
            bar = self._bar(area, horizontal)
            if self._policy_is_off(area, horizontal) or not self._can_move(bar, direction):
                continue
            self._animate(bar, delta, direction)
            event.accept()
            return True
        return False

    def _auto_event(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if isinstance(event, QMouseEvent):
            if kind == QEvent.Type.MouseButtonRelease:
                if event.button() == self._swallowed_button:
                    self._swallowed_button = Qt.MouseButton.NoButton
                    event.accept()
                    return True
            elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                self._swallowed_button = Qt.MouseButton.NoButton
                if self._auto_scroll is not None:
                    self._stop_auto_scroll()
                    self._swallowed_button = event.button()
                    event.accept()
                    return True
                if (
                    event.button() == Qt.MouseButton.MiddleButton
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and not isinstance(watched, QScrollBar)
                    and self._start_auto_scroll(watched, event.globalPosition().toPoint())
                ):
                    self._swallowed_button = event.button()
                    event.accept()
                    return True

        state = self._auto_scroll
        if state is None:
            return False
        if (
            kind == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            event.accept()
            return True
        if kind == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            self._stop_auto_scroll()
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                return True
        elif kind in (QEvent.Type.Wheel, QEvent.Type.ApplicationDeactivate):
            self._stop_auto_scroll()
        elif kind in (
            QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.Resize,
            QEvent.Type.EnabledChange, QEvent.Type.WindowDeactivate,
        ) and isinstance(watched, QWidget):
            if any(
                watched is axis.area
                or watched is axis.area.viewport()
                or watched.isAncestorOf(axis.area)
                for axis in state.axes if isValid(axis.area)
            ):
                self._stop_auto_scroll()
        elif kind == QEvent.Type.Move and isinstance(watched, QWidget):
            # 外层滚动本来就会移动嵌套编辑器；只有原点所在视口/窗口移动才退出。
            if watched is state.marker.parentWidget() or watched is state.marker.window():
                self._stop_auto_scroll()
        return False

    def _start_auto_scroll(self, watched: QObject, origin: QPoint) -> bool:
        candidates = self._scroll_areas(watched)
        axes: list[_AutoAxis] = []
        for horizontal in (False, True):
            for area in candidates:
                if (
                    not area.isVisible() or not area.isEnabled()
                    or area.property("smoothScrollDisabled")
                    or self._policy_is_off(area, horizontal)
                ):
                    continue
                bar = self._bar(area, horizontal)
                if bar.isEnabled() and bar.maximum() > bar.minimum():
                    axes.append(_AutoAxis(area, bar, horizontal))
                    break
        if not axes:
            return False

        # 连同外层的未结束缓动一起取消，避免两个驱动源争抢滚动条。
        for area in candidates:
            self._cancel(area.horizontalScrollBar())
            self._cancel(area.verticalScrollBar())
        horizontal = any(axis.horizontal for axis in axes)
        vertical = any(not axis.horizontal for axis in axes)
        viewport = axes[0].area.viewport()
        marker = _AutoScrollMarker(viewport, horizontal=horizontal, vertical=vertical)
        marker.move(viewport.mapFromGlobal(origin) - QPoint(14, 14))
        state = _AutoScroll(axes, origin, marker)
        self._auto_scroll = state
        for target in {axis.area for axis in axes} | {axis.bar for axis in axes} | {marker}:
            state.connections.append(target.destroyed.connect(self._stop_auto_scroll))
        shape = (
            Qt.CursorShape.SizeAllCursor if horizontal and vertical
            else Qt.CursorShape.SizeHorCursor if horizontal else Qt.CursorShape.SizeVerCursor
        )
        QApplication.setOverrideCursor(QCursor(shape))
        marker.show()
        marker.raise_()
        self._auto_clock.start()
        self._auto_timer.start()
        return True

    def _stop_auto_scroll(self) -> None:
        state = self._auto_scroll
        if state is None:
            return
        self._auto_scroll = None
        self._auto_timer.stop()
        for connection in state.connections:
            QObject.disconnect(connection)
        if isValid(state.marker):
            state.marker.hide()
            state.marker.deleteLater()
        QApplication.restoreOverrideCursor()
        # 状态可能持有顶层页面的最后一个 Python 引用。等当前 Qt 事件分发结束
        # 再释放，避免在 viewport 的 Move/Resize 回调内部析构整个滚动区。
        QTimer.singleShot(0, lambda keep_alive=state: None)

    def _auto_tick(self) -> None:
        state = self._auto_scroll
        if state is None:
            return
        # 无需 mouseTracking 或抓取鼠标：释放中键后仍可跟踪视口外的指针。
        elapsed = min(self._auto_clock.restart() / 1000.0, 0.05)
        self._advance_auto_scroll(QCursor.pos() - state.origin, elapsed)

    def _advance_auto_scroll(self, offset: QPoint, elapsed: float) -> None:
        state = self._auto_scroll
        if state is None:
            return
        for axis in state.axes:
            if (
                not isValid(axis.area) or not isValid(axis.bar)
                or not axis.area.isVisible() or not axis.area.isEnabled()
                or not axis.bar.isEnabled() or self._policy_is_off(axis.area, axis.horizontal)
            ):
                self._stop_auto_scroll()
                return
            distance = offset.x() if axis.horizontal else offset.y()
            outside = abs(distance) - self.AUTO_DEAD_ZONE
            if outside <= 0:
                axis.remainder = 0.0
                continue
            speed = copysign(min(self.AUTO_MAX_SPEED, outside ** 1.4 * 3.0), distance)
            # 文本编辑器/列表可能按行或项滚动，不能把像素速度直接当成项数。
            speed *= max(1, axis.bar.singleStep()) / 20.0
            if axis.remainder * speed < 0:
                axis.remainder = 0.0
            amount = speed * elapsed + axis.remainder
            step = trunc(amount)
            axis.remainder = amount - step
            target = max(axis.bar.minimum(), min(axis.bar.maximum(), axis.bar.value() + step))
            if (
                (target == axis.bar.minimum() and speed < 0)
                or (target == axis.bar.maximum() and speed > 0)
            ):
                axis.remainder = 0.0
            axis.bar.setValue(target)
            # valueChanged 的订阅方可能关闭页面或结束本次自动滚动。
            if self._auto_scroll is not state:
                return

    @staticmethod
    def _wheel_axis(event: QWheelEvent) -> tuple[bool, int]:
        delta = event.angleDelta()
        shifted = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if shifted and delta.y():
            return True, delta.y()
        if abs(delta.x()) > abs(delta.y()):
            return True, delta.x()
        return False, delta.y()

    @staticmethod
    def _scroll_areas(watched: QObject) -> list[QAbstractScrollArea]:
        areas: list[QAbstractScrollArea] = []
        widget = watched if isinstance(watched, QWidget) else None
        while widget is not None:
            if isinstance(widget, QAbstractScrollArea):
                areas.append(widget)
            widget = widget.parentWidget()
        return areas

    @staticmethod
    def _bar(area: QAbstractScrollArea, horizontal: bool) -> QScrollBar:
        return area.horizontalScrollBar() if horizontal else area.verticalScrollBar()

    @staticmethod
    def _policy_is_off(area: QAbstractScrollArea, horizontal: bool) -> bool:
        policy = (
            area.horizontalScrollBarPolicy()
            if horizontal
            else area.verticalScrollBarPolicy()
        )
        return policy == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def _can_move(self, bar: QScrollBar, direction: int) -> bool:
        motion = self._motions.get(bar)
        position = motion.target if motion is not None else bar.value()
        return position > bar.minimum() if direction < 0 else position < bar.maximum()

    def _animate(self, bar: QScrollBar, delta: int, direction: int) -> None:
        current = bar.value()
        previous = self._motions.get(bar)
        base = (
            previous.target
            if previous is not None and previous.direction == direction
            else current
        )
        if previous is not None:
            previous.animation.stop()
            previous.animation.deleteLater()

        app = QApplication.instance()
        lines = app.styleHints().wheelScrollLines() if isinstance(app, QApplication) else 3
        step = max(1, bar.singleStep()) * max(1, lines)
        target = round(base - delta / 120 * step)
        target = max(bar.minimum(), min(bar.maximum(), target))

        animation = QVariantAnimation(self)
        animation.setDuration(self.DURATION_MS)
        animation.setStartValue(current)
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        motion = _Motion(animation, target, direction)
        self._motions[bar] = motion

        animation.valueChanged.connect(
            lambda value, target_bar=bar: self._set_animated_value(target_bar, round(value))
        )
        animation.finished.connect(
            lambda target_bar=bar, expected=animation: self._finish(target_bar, expected)
        )
        animation.start()

    def _set_animated_value(self, bar: QScrollBar, value: int) -> None:
        if isValid(bar):
            bar.setValue(value)
        else:
            self._cancel(bar)

    def _finish(self, bar: QScrollBar, expected: QVariantAnimation) -> None:
        motion = self._motions.get(bar)
        if motion is not None and motion.animation is expected:
            self._motions.pop(bar, None)
            expected.deleteLater()

    def _cancel(self, bar: QScrollBar) -> None:
        motion = self._motions.pop(bar, None)
        if motion is not None:
            motion.animation.stop()
            motion.animation.deleteLater()


def install_smooth_scrolling(app: QApplication) -> SmoothScrollFilter:
    """幂等地把全局滚轮缓动与中键自动滚动装到应用上。"""
    existing = app.findChild(SmoothScrollFilter, _FILTER_NAME)
    if existing is not None:
        return existing
    effect = SmoothScrollFilter(app)
    effect.setObjectName(_FILTER_NAME)
    app.installEventFilter(effect)
    return effect
