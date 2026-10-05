"""消息区的轻量加载遮罩：Qt 定时器只驱动绘制，不参与后台读取。"""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QHideEvent, QPainter, QPaintEvent, QPen, QShowEvent
from PySide6.QtWidgets import QWidget

from limbowave.ui import theme

_FADE_MS = 180


class HistoryLoadingOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._text = "正在加载会话…"
        self._angle = 0
        self._loading = False
        self._opacity = 0.0
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade_anim.valueChanged.connect(self._set_opacity)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAccessibleName(self._text)
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.hide()

    def set_text(self, text: str) -> None:
        self._text = text
        self.setAccessibleName(text)
        self.update()

    def set_loading(self, loading: bool) -> None:
        if loading == self._loading:
            return
        self._loading = loading
        opacity = self._opacity
        self._fade_anim.stop()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not loading)
        if loading:
            self.show()
            self.raise_()
        elif self.isHidden() or self._opacity == 0.0:
            self._set_opacity(0.0)
            self.hide()
            return
        target = 1.0 if loading else 0.0
        self._fade_anim.setDuration(max(1, round(_FADE_MS * abs(target - opacity))))
        self._fade_anim.setStartValue(opacity)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def _set_opacity(self, value: object) -> None:
        self._opacity = float(cast(float, value))
        self.update()

    def _on_fade_finished(self) -> None:
        if not self._loading:
            self.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        parent = self.parentWidget()
        if parent is not None and watched is parent and event.type() == QEvent.Type.Resize:
            self.setGeometry(parent.rect())
        return super().eventFilter(watched, event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._angle = (self._angle + 10) % 360
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._opacity)
        veil = QColor(theme.BG_APP)
        veil.setAlpha(225)
        painter.fillRect(self.rect(), veil)
        width = min(300, self.width() - 24)
        card = QRectF((self.width() - width) / 2, (self.height() - 84) / 2, width, 84)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BG_SURFACE))
        painter.drawRoundedRect(card, 14, 14)
        ring = QRectF(card.left() + 24, card.center().y() - 12, 24, 24)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(theme.BG_ELEVATED), 2.5))
        painter.drawEllipse(ring)
        pen = QPen(QColor(theme.ACCENT), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(ring, -self._angle * 16, 270 * 16)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        text_rect = card.adjusted(64, 0, -16, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, self._text)
