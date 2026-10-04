"""固定栏宽的名称列表：超长文本在行内往返滚动，不参与横向布局。"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QModelIndex,
    QPersistentModelIndex,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QHideEvent, QPainter, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from limbowave.ui import theme

_PAUSE_MS = 900
_PIXELS_PER_SECOND = 28


def _scroll_offset(elapsed_ms: int, overflow: int) -> float:
    """首尾各停顿一次，以固定速度往返，让整段名称都能读到。"""
    if overflow <= 0:
        return 0.0
    travel_ms = overflow * 1000 / _PIXELS_PER_SECOND
    phase = elapsed_ms % (2 * (_PAUSE_MS + travel_ms))
    if phase < _PAUSE_MS:
        return 0.0
    if phase < _PAUSE_MS + travel_ms:
        return (phase - _PAUSE_MS) * _PIXELS_PER_SECOND / 1000
    if phase < 2 * _PAUSE_MS + travel_ms:
        return float(overflow)
    return overflow - (phase - 2 * _PAUSE_MS - travel_ms) * _PIXELS_PER_SECOND / 1000


class _MarqueeDelegate(QStyledItemDelegate):
    def __init__(self, view: MarqueeListWidget) -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        size = super().sizeHint(option, index)
        size.setWidth(0)  # 行宽只由视口决定，保留主题提供的行高与内边距
        return size

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = self._view.style()
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, self._view)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, opt, self._view) + 1
        text_rect.adjust(margin, 0, -margin, 0)
        text_width = opt.fontMetrics.horizontalAdvance(opt.text)
        overflow = max(0, text_width - text_rect.width())
        offset = self._view._offset_for(index, overflow)
        if not overflow:
            super().paint(painter, option, index)
            return

        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, self._view)
        painter.save()
        painter.setClipRect(text_rect, Qt.ClipOperation.IntersectClip)
        painter.setFont(opt.font)
        # 列表主题在普通、悬停、选中三态均使用主文字色，背景仍交给 Qt/QSS。
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.translate(-offset, 0)
        text_rect.setWidth(text_width)
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text
        )
        painter.restore()


class MarqueeListWidget(QListWidget):
    """只为当前可见的超长行计时；隐藏、重载或调整大小后从头阅读。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsNameList")
        self._scrolling: dict[QPersistentModelIndex, tuple[int, QElapsedTimer]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)
        self.setItemDelegate(_MarqueeDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setWordWrap(False)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSizeAdjustPolicy(QListWidget.SizeAdjustPolicy.AdjustIgnored)
        self._current_indicator = QFrame(self.viewport())
        self._current_indicator.setObjectName("settingsCurrentIndicator")
        self._current_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._current_indicator.hide()
        self._indicator_animation = QPropertyAnimation(self._current_indicator, b"geometry", self)
        self._indicator_animation.setDuration(260)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.currentItemChanged.connect(lambda: self._sync_current_indicator(animated=True))
        self.model().modelReset.connect(self._reset_scroll)
        self.model().dataChanged.connect(self._reset_scroll)
        self.model().rowsRemoved.connect(self._reset_scroll)
        self.model().rowsInserted.connect(self._reset_scroll)
        self.model().layoutChanged.connect(self._reset_scroll)

    def _offset_for(
        self, index: QModelIndex | QPersistentModelIndex, overflow: int
    ) -> float:
        key = QPersistentModelIndex(index)
        if overflow <= 0 or not self.isVisible():
            self._scrolling.pop(key, None)
            if not self._scrolling:
                self._timer.stop()
            return 0.0
        previous = self._scrolling.get(key)
        if previous is None or previous[0] != overflow:
            clock = QElapsedTimer()
            clock.start()
            self._scrolling[key] = (overflow, clock)
        else:
            clock = previous[1]
        if not self._timer.isActive():
            self._timer.start()
        return _scroll_offset(clock.elapsed(), overflow)

    def _advance(self) -> None:
        viewport = self.viewport()
        for index in list(self._scrolling):
            if not index.isValid() or not self.visualRect(index).intersects(viewport.rect()):
                del self._scrolling[index]
        if not self._scrolling:
            self._timer.stop()
            return
        viewport.update()

    def _reset_scroll(self) -> None:
        self._timer.stop()
        self._scrolling.clear()
        self.viewport().update()
        self._sync_current_indicator()

    def _sync_current_indicator(self, *, animated: bool = False) -> None:
        # 当前编辑项不等于多选集合；失焦到右侧表单后仍保留指示。
        row = self.visualRect(self.currentIndex())
        height = min(20, row.height() - 8)
        if not self.isVisible() or height <= 0 or not row.intersects(self.viewport().rect()):
            self._indicator_animation.stop()
            self._current_indicator.hide()
            return
        target = QRect(row.left() + 2, row.top() + (row.height() - height) // 2, 3, height)
        current = self._current_indicator.geometry()
        self._indicator_animation.stop()
        if animated and self._current_indicator.isVisible() and current != target:
            # 中途纵向轻拉伸（最多 48px），保持原来的中心位移与总时长。
            # 从当前几何形状续接，快速切换时不跳变，也不累积拉伸幅度。
            stretch = min(48, abs(target.center().y() - current.center().y()))
            middle_height = max(current.height(), target.height() + stretch)
            center_y = (
                current.y() + current.height() / 2 + target.y() + target.height() / 2
            ) / 2
            middle = QRect(
                (current.x() + target.x()) // 2,
                round(center_y - middle_height / 2),
                target.width(),
                middle_height,
            )
            self._indicator_animation.setStartValue(current)
            self._indicator_animation.setKeyValueAt(0.5, middle)
            self._indicator_animation.setEndValue(target)
            self._indicator_animation.start()
        else:
            self._current_indicator.setGeometry(target)
        self._current_indicator.show()
        self._current_indicator.raise_()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_current_indicator()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._sync_current_indicator()

    def doItemsLayout(self) -> None:
        super().doItemsLayout()
        if hasattr(self, "_current_indicator"):
            self._sync_current_indicator()

    def hideEvent(self, event: QHideEvent) -> None:
        self._reset_scroll()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reset_scroll()
