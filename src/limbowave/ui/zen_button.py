"""A paired, gently expanding companion / Zen entry without a header row."""
from __future__ import annotations

from typing import cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QLineF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFocusEvent,
    QFont,
    QFontMetrics,
    QHideEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from limbowave.ui import theme

_SIZE = 28
_REVEAL_MS = 220


class _ModeSegment(QPushButton):
    """Keep native button hit testing, focus and accessibility for each action."""

    def __init__(self, capsule: ModeCapsule, caption: str, *, zen: bool = False) -> None:
        super().__init__(capsule)
        self._capsule = capsule
        self.caption = caption
        self._zen = zen
        self._hovered = False
        self._keyboard_focus = False
        self.setCheckable(zen)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("themeEffectDisabled", True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")
        self.setAccessibleName(caption)
        self.toggled.connect(capsule.update)
        self.pressed.connect(capsule.update)
        self.released.connect(capsule.update)

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._hovered = True
        self._capsule.update()
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self._capsule.update()
        self.update()

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self._capsule._reveal()
        self.update()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self._keyboard_focus = False
        # Let the other half receive focus before deciding to collapse.
        QTimer.singleShot(0, self._capsule._reveal)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = (theme.ACCENT if self.isChecked() else
                 theme.TEXT_PRIMARY if self._hovered else theme.TEXT_SECONDARY)
        if self._keyboard_focus:
            painter.setPen(QPen(QColor(theme.ACCENT), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 10, 10)
        progress = self._capsule._progress
        painter.setOpacity(max(0.0, (progress - 0.25) / 0.75))
        painter.setFont(self._capsule._label_font())
        # Keep small captions readable; the segment tint and book indicate the toggle.
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.caption)
        painter.setOpacity(max(0.0, 1.0 - progress * 2))
        painter.setPen(QPen(QColor(color), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.translate((self.width() - 16) / 2, (self.height() - 16) / 2)
        path = QPainterPath()
        if self._zen:
            # An open book: two pages and a central fold.
            path.moveTo(8, 3.5)
            path.quadTo(4.5, 1, 1.5, 2.5)
            path.lineTo(1.5, 12.5)
            path.quadTo(4.5, 11, 8, 13.5)
            path.quadTo(11.5, 11, 14.5, 12.5)
            path.lineTo(14.5, 2.5)
            path.quadTo(11.5, 1, 8, 3.5)
            path.lineTo(8, 13.5)
        else:
            # A speech bubble, not a leaf or a second window icon.
            path.moveTo(4, 2)
            path.lineTo(12, 2)
            path.quadTo(14, 2, 14, 4)
            path.lineTo(14, 10)
            path.quadTo(14, 12, 12, 12)
            path.lineTo(7, 12)
            path.lineTo(3, 14)
            path.lineTo(3.5, 11.5)
            path.quadTo(2, 11, 2, 10)
            path.lineTo(2, 4)
            path.quadTo(2, 2, 4, 2)
        painter.drawPath(path)
        painter.end()


class ModeCapsule(QWidget):
    """Two actions share one surface, stationary divider and reversible reveal timeline."""

    extent_changed = Signal()
    collapsed_width = _SIZE * 2 + 1

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._hovered = False
        self.setProperty("themeEffectDisabled", True)
        self.setAccessibleName("陪伴与倾诉模式")
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(_REVEAL_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._set_progress)
        self.companion_button = _ModeSegment(self, "迷你陪伴")
        self.companion_button.setToolTip("迷你陪伴：打开轻量聊天窗口（Ctrl+Shift+M）")
        self.zen_button = _ModeSegment(self, "倾诉模式", zen=True)
        self.zen_button.setToolTip("倾诉模式（纯净模式）：隐藏技术信息，专注对话（Ctrl+Shift+F）")
        QWidget.setTabOrder(self.companion_button, self.zen_button)
        self._set_progress(0.0)

    def _label_font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(theme.FS_SMALL)
        return font

    @property
    def expanded_width(self) -> int:
        metrics = QFontMetrics(self._label_font())
        return 2 * max(_SIZE, max(metrics.horizontalAdvance(button.caption) for button in (
            self.companion_button, self.zen_button,
        )) + 20) + 1

    def _set_progress(self, value: object) -> None:
        self._progress = float(cast(float, value))
        segment_width = _SIZE + round(
            ((self.expanded_width - 1) / 2 - _SIZE) * self._progress
        )
        self.setFixedSize(segment_width * 2 + 1, _SIZE)
        self.companion_button.setGeometry(0, 0, segment_width, _SIZE)
        self.zen_button.setGeometry(segment_width + 1, 0, segment_width, _SIZE)
        self.extent_changed.emit()
        self.update()
        self.companion_button.update()
        self.zen_button.update()

    def _reveal(self) -> None:
        expanded = self._hovered or any(button._keyboard_focus for button in (
            self.companion_button, self.zen_button,
        ))
        target = float(expanded) if self.isVisible() else 0.0
        if not self.isVisible() or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General):
            self._animation.stop()
            self._set_progress(target)
            return
        animation = self._animation
        if animation.state() == QAbstractAnimation.State.Stopped and self._progress == target:
            return
        animation.setDirection(QAbstractAnimation.Direction.Forward if expanded
                               else QAbstractAnimation.Direction.Backward)
        if animation.state() == QAbstractAnimation.State.Stopped:
            animation.start()
        elif animation.state() == QAbstractAnimation.State.Paused:
            animation.resume()

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._hovered = True
        self._reveal()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self._reveal()

    def hideEvent(self, event: QHideEvent) -> None:
        self._animation.stop()
        self._hovered = False
        for button in (self.companion_button, self.zen_button):
            button._hovered = False
            button._keyboard_focus = False
        self._set_progress(0.0)
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if hasattr(self, "zen_button") and event.type() in (
            QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange,
        ):
            self._set_progress(self._progress)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        painter.setClipPath(shape)
        painter.fillPath(shape, QColor(theme.BG_SURFACE))
        for button in (self.companion_button, self.zen_button):
            if button.isChecked() or button._hovered or button.isDown():
                tint = QColor(theme.ACCENT)
                tint.setAlphaF(0.15 if button.isDown() else 0.09)
                painter.fillRect(button.geometry(), tint)
        border = QColor(theme.TEXT_SECONDARY)
        border.setAlphaF(0.22)
        painter.setPen(QPen(border, 1))
        painter.drawPath(shape)
        middle = self.companion_button.width() + 0.5
        painter.drawLine(QLineF(middle, 7, middle, self.height() - 7))
        painter.end()
