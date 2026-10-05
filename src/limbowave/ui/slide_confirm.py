"""可复用的拖动确认控件：必须从手柄起拖，点击轨道或半程松手均不确认。"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QHideEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from limbowave.ui import theme

SLIDER_SNAP_MS = 160


class SlideConfirm(QWidget):
    """从左滑到最右才确认的滑块（防误触）。

    拖动手柄到最右端即发出 ``confirmed``；中途松开弹回起点。
    自绘轨道、进度、手柄与提示文字——不用字体字符，缺字不会退化成方框。
    仅从左侧手柄起拖才有效，点击轨道不会确认。进出场动画由宿主负责。
    """

    _INSET = 3.0  # 轨道内手柄四周的留白

    confirmed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._progress = 0.0  # 0 = 手柄在最左，1 = 滑到最右（确认）
        self._dragging = False
        self._drag_offset = 0.0
        self._confirmed = False
        self._hint = ""
        self._snap: QVariantAnimation | None = None

    # ---------- 状态 ----------

    @property
    def progress(self) -> float:
        return self._progress

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.setAccessibleName(text)
        self.setToolTip("按住左侧手柄拖到最右；中途松开取消")
        self.update()

    def reset(self) -> None:
        """回到初始态：手柄归零、丢弃进行中的手势与弹回动画（进出场前后调用）。"""
        self._stop_snap()
        self._dragging = False
        self._confirmed = False
        self._set_progress(0.0)

    def snap_back(self) -> None:
        """手柄弹回起点：松手未滑到最右、或整条退场动画开始时。"""
        if self._progress <= 0.0:
            return
        self._stop_snap()
        anim = QVariantAnimation(self)
        anim.setStartValue(self._progress)
        anim.setEndValue(0.0)
        anim.setDuration(SLIDER_SNAP_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda value: self._set_progress(float(value)))
        anim.finished.connect(lambda: self._on_snap_done(anim))
        self._snap = anim
        anim.start()

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    def _stop_snap(self) -> None:
        if self._snap is not None:
            anim, self._snap = self._snap, None
            anim.stop()  # finished 回调里 _snap 已不指向它，只做 deleteLater
            anim.deleteLater()

    def _on_snap_done(self, anim: QVariantAnimation) -> None:
        if self._snap is anim:
            self._snap = None
        anim.deleteLater()

    # ---------- 几何 ----------

    def _diameter(self) -> float:
        return self.height() - 2 * self._INSET

    def _span(self) -> float:
        """手柄可移动的距离（最左到最右）。"""
        return max(1.0, self.width() - 2 * self._INSET - self._diameter())

    def _handle_rect(self) -> QRectF:
        x = self._INSET + self._span() * self._progress
        return QRectF(x, self._INSET, self._diameter(), self._diameter())

    # ---------- 手势 ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._confirmed or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # 新手势始终从起点开始，不能接住弹回中的手柄累积进度。
        self.reset()
        if not self._handle_rect().contains(event.position()):
            event.ignore()
            return
        self._drag_offset = event.position().x() - self._handle_rect().center().x()
        self._dragging = True
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or self._confirmed:
            super().mouseMoveEvent(event)
            return
        self._track(event.position().x() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and not self._confirmed and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.snap_back()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _track(self, x: float) -> None:
        """按指针位置推进手柄；滑到最右即确认（一次性的，确认后忽略后续手势）。"""
        progress = (x - self._INSET - self._diameter() / 2) / self._span()
        self._set_progress(max(0.0, min(1.0, progress)))
        if self._progress >= 1.0:
            self._dragging = False
            self._confirmed = True
            self.confirmed.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        self.reset()
        super().hideEvent(event)

    # ---------- 绘制 ----------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = track.height() / 2
        painter.setPen(QPen(QColor(theme.BORDER), 1.0))
        painter.setBrush(QColor(theme.BG_SURFACE_HOVER))
        painter.drawRoundedRect(track, radius, radius)

        handle = self._handle_rect()
        # 已滑过的进度：accent 淡色铺在手柄扫过的轨道里
        if self._progress > 0:
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(track, radius, radius)
            painter.setClipPath(clip)
            fill = QColor(theme.ACCENT)
            fill.setAlpha(42)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(
                QRectF(
                    track.left(),
                    track.top(),
                    handle.center().x() - track.left(),
                    track.height(),
                )
            )
            painter.restore()

        # 提示文字随手柄推进渐隐，把位置让给「正在确认」的动势
        if self._hint and self._progress < 0.9:
            text_rect = QRectF(
                handle.right() + 8,
                track.top(),
                track.right() - handle.right() - 8,
                track.height(),
            )
            if text_rect.width() > 24:
                font = painter.font()
                font.setPixelSize(theme.FS_SMALL)
                painter.setFont(font)
                painter.setOpacity(1.0 - self._progress / 0.9)
                painter.setPen(QColor(theme.TEXT_SECONDARY))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._hint)
                painter.setOpacity(1.0)

        # 手柄：accent 描边的圆钮 + 双箭头，示意「向右」
        painter.setPen(QPen(QColor(theme.ACCENT), 1.2))
        painter.setBrush(QColor(theme.BG_ELEVATED))
        painter.drawEllipse(handle)
        painter.setPen(
            QPen(
                QColor(theme.ACCENT),
                1.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        center = handle.center()
        for offset in (-2.8, 1.2):
            painter.drawLine(
                QPointF(center.x() + offset - 1.6, center.y() - 3.0),
                QPointF(center.x() + offset + 1.2, center.y()),
            )
            painter.drawLine(
                QPointF(center.x() + offset + 1.2, center.y()),
                QPointF(center.x() + offset - 1.6, center.y() + 3.0),
            )
        painter.end()
