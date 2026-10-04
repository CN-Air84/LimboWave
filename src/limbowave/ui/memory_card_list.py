"""双栏记忆卡片：保留原生列表选择与键盘交互，只改变布局和绘制。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QResizeEvent,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from limbowave.ui import theme


class _MemoryCardDelegate(QStyledItemDelegate):
    def __init__(self, view: MemoryCardList) -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return self._view.gridSize()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(opt.state & QStyle.StateFlag.State_HasFocus)
        card = opt.rect.adjusted(5, 5, -5, -5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(opt.rect)
        background = (
            theme.BG_ELEVATED
            if selected
            else theme.BG_SURFACE_HOVER
            if hovered
            else theme.BG_SURFACE
        )
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(theme.ACCENT if selected or focused else theme.BORDER)))
        painter.drawRoundedRect(card, theme.RADIUS_MD, theme.RADIUS_MD)

        text_rect = card.adjusted(14, 14, -14, -14)
        title_font = QFont(opt.font)
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        title_rect = QRect(
            text_rect.left(),
            text_rect.top(),
            text_rect.width(),
            title_metrics.height(),
        )
        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title_metrics.elidedText(opt.text, Qt.TextElideMode.ElideRight, text_rect.width()),
        )

        memory = index.data(Qt.ItemDataRole.UserRole)
        preview = " ".join(memory.content.splitlines()[1:]).strip()
        if preview and text_rect.width() > 0:
            painter.setFont(opt.font)
            painter.setPen(QColor(theme.TEXT_SECONDARY))
            metrics = QFontMetrics(opt.font)
            wrapped = QTextLayout(preview, opt.font)
            text_option = wrapped.textOption()
            text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            wrapped.setTextOption(text_option)
            encoded = preview.encode("utf-16-le")
            wrapped.beginLayout()
            top = title_rect.bottom() + 9
            for row in range(2):
                line = wrapped.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(text_rect.width())
                # QTextLayout 的偏移按 UTF-16 计数，不能直接切 Python 字符串（如 emoji）。
                start = line.textStart() * 2
                end = start + line.textLength() * 2 if row == 0 else len(encoded)
                text = encoded[start:end].decode("utf-16-le")
                if row == 1:
                    text = metrics.elidedText(
                        text,
                        Qt.TextElideMode.ElideRight,
                        text_rect.width(),
                    )
                painter.drawText(
                    QRect(text_rect.left(), top, text_rect.width(), metrics.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text,
                )
                top += metrics.lineSpacing()
            wrapped.endLayout()
        painter.restore()


class MemoryCardList(QListWidget):
    """视口宽度决定两列等宽网格，长内容不撑宽列表或卡片。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("memoryCardList")
        self.setAccessibleName("记忆卡片")
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QListWidget.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 固定预留滚动条宽度，避免 Qt 在条目较少时预估布局而退回单列。
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(_MemoryCardDelegate(self))
        self.empty_state = QLabel("暂无记忆\n点击“添加记忆”创建第一条记忆。", self.viewport())
        self.empty_state.setObjectName("memoryPageHint")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.empty_state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.model().rowsInserted.connect(self._sync_grid)
        self.model().rowsRemoved.connect(self._sync_grid)
        self.model().modelReset.connect(self._sync_grid)
        self._sync_grid()

    def _sync_grid(self) -> None:
        self.setGridSize(
            QSize(
                max(1, (self.viewport().width() - 1) // 2),
                self.fontMetrics().lineSpacing() * 3 + 48,
            )
        )
        self.empty_state.setGeometry(self.viewport().rect())
        self.empty_state.setVisible(self.count() == 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_grid()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
            self, "empty_state"
        ):
            self._sync_grid()
