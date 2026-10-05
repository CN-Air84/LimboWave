"""沿用状态条强调色的轻量双层水波，不表示确定的完成进度。"""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QHideEvent, QPainter, QPainterPath, QPaintEvent, QPen, QShowEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from limbowave.ui import theme


class RunStateLabel(QLabel):
    """只重绘背景；保留 QLabel 的换行、尺寸计算和无障碍文字。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("assistantRunState")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setContentsMargins(10, 6, 10, 6)
        self.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
            f" border: 1px solid transparent; border-radius: {theme.RADIUS_SM}px;"
            f" font-size: {theme.FS_SMALL}px;"
        )
        # A blur effect would rasterize the entire animated label on every frame.
        self.setProperty("themeGlowDisabled", True)
        self._animated = True
        self._geometry_key: tuple[int, int, float] | None = None
        self._clip = QPainterPath()
        self._waves: list[QPainterPath] = []
        self._phase = 0.0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_animation()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def set_animated(self, animated: bool) -> None:
        if self._animated == animated:
            return
        self._animated = animated
        self._sync_animation()
        self.update()

    def _sync_animation(self) -> None:
        if (
            self._animated and self.isVisible()
            and QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
        ):
            if not self._timer.isActive():
                self._clock.start()
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        # QWidget.isVisible() stays true outside a QScrollArea viewport. Do not
        # invalidate transparent ancestors (including the wallpaper) offscreen.
        if self.visibleRegion().isEmpty() or self.window().isMinimized():
            return
        if not QApplication.isEffectEnabled(Qt.UIEffect.UI_General):
            self._timer.stop()
            return
        self._phase = (self._clock.elapsed() % 6000) / 6000 * math.tau
        self.update()

    def _ensure_geometry(self) -> None:
        key = (self.width(), self.height(), float(theme.RADIUS_SM))
        if self._geometry_key == key:
            return
        self._geometry_key = key
        self._clip = QPainterPath()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        self._clip.addRoundedRect(rect, theme.RADIUS_SM, theme.RADIUS_SM)
        self._waves = []
        # One extra period allows translation instead of rebuilding hundreds of
        # Python sine samples per frame. Geometry is shared across scroll paints.
        for level, amplitude, wavelength in ((0.61, 0.13, 300), (0.73, 0.10, 220)):
            wave = QPainterPath()
            wave.moveTo(0, self.height())
            end = self.width() + wavelength + 4
            for x in range(0, end + 4, 4):
                y = self.height() * level + min(self.height(), 48) * amplitude * math.sin(
                    x / wavelength * math.tau
                )
                wave.lineTo(x, y)
            wave.lineTo(end + 4, self.height())
            wave.closeSubpath()
            self._waves.append(wave)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._ensure_geometry()
        clip = self._clip
        accent = QColor(theme.ACCENT)
        accent.setAlphaF(0.10)
        painter.fillPath(clip, accent)
        painter.save()
        painter.setClipPath(clip)
        for wave, wavelength, phase, alpha in (
            (self._waves[0], 300.0, self._phase, 0.08),
            (self._waves[1], 220.0, -self._phase + 1.8, 0.12),
        ):
            painter.save()
            painter.translate(-(phase % math.tau) / math.tau * wavelength, 0)
            accent.setAlphaF(alpha)
            painter.fillPath(wave, accent)
            painter.restore()
        painter.restore()
        accent.setAlphaF(0.24)
        painter.setPen(QPen(accent, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(clip)
        painter.end()
        # 透明 QLabel 在波纹之上绘制文字，避免文字也参与动效。
        super().paintEvent(event)
