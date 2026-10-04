"""已发送消息的附件缩略图：挂在用户气泡上方。

发送后附件栏会清空；这里让用户仍能看到这一轮带了哪些附件。
图片显示缩略图，点击放大预览；文档显示名称 chip。悬停给出详情。

只做展示：附件数据由上层（app 组装层）解析成 :class:`SentAttachment` 传进来。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme

_THUMB = 56
_CHIP_TEXT_WIDTH = 180


def _elide(label: QLabel, text: str, mode: Qt.TextElideMode) -> str:
    label.ensurePolished()  # 先套上样式表的字号，量出来的宽度才准
    return label.fontMetrics().elidedText(text, mode, _CHIP_TEXT_WIDTH)


@dataclass(frozen=True, slots=True)
class SentAttachment:
    """一条已发送附件的展示数据。"""

    title: str
    detail: str
    thumbnail: QPixmap | None = None  # 图片原图；None 表示文档


class ImagePreviewDialog(QDialog):
    """图片放大预览：不超过屏幕可用区的八成，超出按比例缩小。"""

    def __init__(self, item: SentAttachment, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(item.title)
        assert item.thumbnail is not None
        pixmap = item.thumbnail
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            max_w, max_h = int(area.width() * 0.8), int(area.height() * 0.8)
            if pixmap.width() > max_w or pixmap.height() > max_h:
                pixmap = pixmap.scaled(
                    max_w,
                    max_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        image = QLabel()
        image.setPixmap(pixmap)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(image)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, 1)
        detail = QLabel(item.detail)
        detail.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;")
        layout.addWidget(detail)
        self.resize(pixmap.width() + 28, pixmap.height() + 56)


class _SentAttachmentTile(QFrame):
    """单个附件：图片为方形缩略图，文档为名称 chip。"""

    def __init__(self, item: SentAttachment, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self.setObjectName("sentAttachment")
        self.setToolTip(f"{item.title}\n{item.detail}" if item.detail else item.title)
        self.setStyleSheet(
            f"QFrame#sentAttachment {{ background: {theme.card_surface()};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; }}"
            "QFrame#sentAttachment QLabel { border: none; background: transparent; }"
        )
        layout = QHBoxLayout(self)
        if item.thumbnail is not None:
            layout.setContentsMargins(0, 0, 0, 0)
            self.setFixedSize(_THUMB, _THUMB)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            thumb = QLabel()
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setPixmap(
                item.thumbnail.scaled(
                    _THUMB - 4,
                    _THUMB - 4,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(thumb)
        else:
            layout.setContentsMargins(10, 6, 10, 6)
            self.setFixedHeight(_THUMB)
            self.setMaximumWidth(200)
            col = QVBoxLayout()
            col.setSpacing(1)
            title = QLabel()
            title.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_SMALL}px;")
            title.setText(_elide(title, item.title, Qt.TextElideMode.ElideMiddle))
            col.addWidget(title)
            detail = QLabel()
            detail.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px;")
            detail.setText(_elide(detail, item.detail, Qt.TextElideMode.ElideRight))
            col.addWidget(detail)
            layout.addLayout(col)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            self._item.thumbnail is not None
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            ImagePreviewDialog(self._item, self.window()).exec()
            return
        super().mouseReleaseEvent(event)


class SentAttachmentStrip(QWidget):
    """一排靠右的附件缩略图。"""

    def __init__(self, items: list[SentAttachment], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)
        for item in items:
            layout.addWidget(_SentAttachmentTile(item))
