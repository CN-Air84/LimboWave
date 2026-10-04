"""全局复选框指示器：任何状态都有可见方框，勾选与取消都有过渡。

原生（windows11）指示器的问题：勾选后只剩一块强调色填充、没有方框轮廓，
悬停还会整片铺强调色底——用户分不清「这是能点的框」还是「一块色标」。
这里用代理样式接管 ``PE_IndicatorCheckBox``，所有 QCheckBox（包括第三方/Qt
自带对话框里创建的）统一绘制，不需要每个视图单独设置。

- 方框：静止描边与下拉框同色（``theme.combo_frame_colors``），悬停只加深描边、不换底色；
- 勾选：强调色填充淡入 + 对勾按笔画长度逐段画出；取消时原路退回，而不是瞬间消失；
- 过渡状态挂在每个复选框的直接子对象上，样式对象本身保持无状态。
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QCheckBox, QProxyStyle, QStyle, QStyleOption, QWidget
from shiboken6 import isValid

from limbowave.ui import theme

_TRANSITION_NAME = "limbowaveCheckTransition"
_INDICATOR_SIZE = 16
_RADIUS = 4.0

# 对勾与半选横线，坐标以方框边长为单位。
_CHECK_POINTS = ((0.26, 0.53), (0.43, 0.69), (0.75, 0.35))
_DASH_POINTS = ((0.28, 0.5), (0.72, 0.5))


class _CheckTransition(QObject):
    """One checkbox's animated levels: ``fill`` (0 未选 → 1 已选) 与 ``hover``。"""

    CHECK_MS = 150
    UNCHECK_MS = 190
    HOVER_ENTER_MS = 120
    HOVER_RESTORE_MS = 220

    def __init__(self, box: QCheckBox, fill: float, glyph: Qt.CheckState) -> None:
        super().__init__(box)
        self.setObjectName(_TRANSITION_NAME)
        self.box = box
        self.fill = self._fill_target = fill
        self.hover = 0.0
        # 取消勾选的退场过程中仍要画「刚才那个」符号，而不是当前的 Unchecked。
        self.glyph = glyph
        self._fill_animation = self._animation(self._set_fill)
        self._hover_animation = self._animation(self._set_hover)
        box.installEventFilter(self)

    def _animation(self, slot: object) -> QVariantAnimation:
        animation = QVariantAnimation(self)
        animation.valueChanged.connect(slot)
        return animation

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.box and isValid(self.box):
            kind = event.type()
            if kind == QEvent.Type.Enter:
                self._retarget_hover(1.0 if self.box.isEnabled() else 0.0)
            elif kind == QEvent.Type.Leave:
                self._retarget_hover(0.0)
            elif kind == QEvent.Type.EnabledChange:
                self._retarget_hover(1.0 if self.box.underMouse() and self.box.isEnabled() else 0.0)
            elif kind == QEvent.Type.Hide:
                self._hover_animation.stop()
                self.hover = 0.0
        return False

    def sync(self, fill: float, glyph: Qt.CheckState) -> None:
        """Called from paint with the state Qt is about to draw; starts a transition on change."""
        if glyph != Qt.CheckState.Unchecked:
            self.glyph = glyph
        if fill == self._fill_target:
            return
        self._fill_target = fill
        if not self.box.isVisible():
            self._fill_animation.stop()
            self.fill = fill
            return
        self._run(self._fill_animation, self.fill, fill, self.CHECK_MS, self.UNCHECK_MS)

    def _retarget_hover(self, target: float) -> None:
        if self._hover_animation.endValue() == target and (
            self._hover_animation.state() == QAbstractAnimation.State.Running
            or self.hover == target
        ):
            return
        self._run(
            self._hover_animation, self.hover, target, self.HOVER_ENTER_MS, self.HOVER_RESTORE_MS
        )

    @staticmethod
    def _run(
        animation: QVariantAnimation, start: float, end: float, enter_ms: int, restore_ms: int
    ) -> None:
        animation.stop()
        # 半路反向时按剩余距离折算时长，来回快速点击不会拖慢。
        full = enter_ms if end > start else restore_ms
        animation.setDuration(max(1, round(full * abs(end - start))))
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.start()

    def _set_fill(self, value: object) -> None:
        self.fill = float(value)  # type: ignore[arg-type]
        if isValid(self.box):
            self.box.update()

    def _set_hover(self, value: object) -> None:
        self.hover = float(value)  # type: ignore[arg-type]
        if isValid(self.box):
            self.box.update()


def _transition_for(box: QCheckBox, fill: float, glyph: Qt.CheckState) -> _CheckTransition:
    existing = box.findChild(QObject, _TRANSITION_NAME, Qt.FindChildOption.FindDirectChildrenOnly)
    if isinstance(existing, _CheckTransition):
        return existing
    # 首次绘制直接落在当前状态：窗口打开时不应该播放一遍勾选动画。
    return _CheckTransition(box, fill, glyph)


def _check_state(option: QStyleOption) -> Qt.CheckState:
    if option.state & QStyle.StateFlag.State_NoChange:
        return Qt.CheckState.PartiallyChecked
    if option.state & QStyle.StateFlag.State_On:
        return Qt.CheckState.Checked
    return Qt.CheckState.Unchecked


def _mix(left: QColor, right: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(
        left.redF() + (right.redF() - left.redF()) * amount,
        left.greenF() + (right.greenF() - left.greenF()) * amount,
        left.blueF() + (right.blueF() - left.blueF()) * amount,
        left.alphaF() + (right.alphaF() - left.alphaF()) * amount,
    )


def _partial_path(box: QRectF, points: tuple[tuple[float, float], ...], progress: float) -> QPainterPath:
    """按总笔画长度截取折线的前 ``progress`` 部分——对勾是「画出来」的。"""
    mapped = [QPointF(box.x() + x * box.width(), box.y() + y * box.height()) for x, y in points]
    lengths = [
        math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(mapped, mapped[1:], strict=False)
    ]
    remaining = sum(lengths) * max(0.0, min(1.0, progress))
    path = QPainterPath(mapped[0])
    for start, end, length in zip(mapped, mapped[1:], lengths, strict=False):
        if remaining <= 0 or length == 0:
            break
        ratio = min(1.0, remaining / length)
        path.lineTo(start + (end - start) * ratio)
        remaining -= length
    return path


def paint_indicator(
    painter: QPainter,
    rect: QRectF,
    *,
    fill: float,
    hover: float,
    glyph: Qt.CheckState,
    enabled: bool,
) -> None:
    side = min(rect.width(), rect.height())
    box = QRectF(
        rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2, side, side
    )
    rest, emphasis, _ = (QColor(c) for c in theme.combo_frame_colors())
    accent = QColor(theme.ACCENT)
    frame = _mix(rest, emphasis, hover)
    mark = QColor(theme.TEXT_ON_ACCENT)
    if not enabled:
        frame = QColor(theme.BORDER)
        accent = _mix(QColor(theme.BORDER), QColor(theme.TEXT_SECONDARY), 0.35)
        mark = QColor(theme.BG_APP)
    background = QColor(accent)
    background.setAlphaF(fill)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(_mix(frame, accent, fill), 1.0)
    painter.setPen(pen)
    painter.setBrush(background)
    outline = box.adjusted(0.5, 0.5, -0.5, -0.5)
    painter.drawRoundedRect(outline, _RADIUS, _RADIUS)

    if fill > 0 and glyph != Qt.CheckState.Unchecked:
        points = _DASH_POINTS if glyph == Qt.CheckState.PartiallyChecked else _CHECK_POINTS
        # 底色先到位、对勾随后画出：对勾只占过渡的后 70%，退场时则先收回对勾。
        progress = (fill - 0.3) / 0.7
        if progress > 0:
            stroke = QPen(mark, max(1.6, side * 0.12))
            stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(stroke)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(_partial_path(box, points, progress))
    painter.restore()


class CheckBoxStyle(QProxyStyle):
    """Application proxy style that owns checkbox indicators; everything else passes through."""

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight):
            return round(_INDICATOR_SIZE * theme.current_font_scale())
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element not in (
            QStyle.PrimitiveElement.PE_IndicatorCheckBox,
            QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
        ):
            super().drawPrimitive(element, option, painter, widget)
            return
        state = _check_state(option)
        target = 0.0 if state == Qt.CheckState.Unchecked else 1.0
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        fill, hover, glyph = target, 0.0, state
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox and isinstance(
            widget, QCheckBox
        ):
            transition = _transition_for(widget, target, state)
            transition.sync(target, state)
            fill, hover, glyph = transition.fill, transition.hover, transition.glyph
        paint_indicator(
            painter,
            QRectF(option.rect),
            fill=fill,
            hover=hover if enabled else 0.0,
            glyph=glyph,
            enabled=enabled,
        )
