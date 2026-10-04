"""附件栏：输入区上方的附件 chips + 添加按钮（设计计划 §三.1：输入区有「添加附件」）。

每个附件是一个 chip：图片显示缩略图 + 格式/尺寸/大小/估算占用，
文本/剪贴板文档显示名称 + 行数。chip 带移除按钮。

架构约束：附件栏只持有**待发送**的展示态（用户已选但还没发送的附件），
发送后由上层清空。附件的权威数据（登记卡、blob）在服务层。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.popup_material import install_popup_material
from limbowave.ui.popup_motion import PopupMotion


class _AttachmentChip(QFrame):
    """单个附件 chip。图片带缩略图，文本带行数。"""

    remove_clicked = Signal(str)  # attachment_id

    def __init__(
        self,
        attachment_id: str,
        title: str,
        subtitle: str,
        *,
        thumbnail: QPixmap | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._attachment_id = attachment_id
        self.setStyleSheet(
            f"QFrame {{ background: {theme.card_surface()}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_MD}px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 6, 6)
        layout.setSpacing(8)

        if thumbnail is not None:
            thumb = QLabel()
            thumb.setPixmap(
                thumbnail.scaled(
                    40,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            thumb.setFixedSize(40, 40)
            thumb.setStyleSheet(f"border-radius: {theme.RADIUS_SM}px; border: none;")
            layout.addWidget(thumb)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_SMALL}px; border: none;"
        )
        text_col.addWidget(title_label)
        subtitle_label = QLabel(subtitle)
        subtitle_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
        )
        text_col.addWidget(subtitle_label)
        layout.addLayout(text_col)

        remove_btn = QPushButton("×")
        remove_btn.setFixedSize(20, 20)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setToolTip("移除附件")
        # padding 必须清零：全局 QPushButton 的 7px 16px 内边距会把 20px 宽按钮里的 × 挤没
        remove_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; font-size: {theme.FS_TITLE}px;"
            f" color: {theme.TEXT_SECONDARY}; border-radius: 10px; padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.DANGER_BG}; color: {theme.DANGER_TEXT}; }}"
        )
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self._attachment_id))
        layout.addWidget(remove_btn, 0, Qt.AlignmentFlag.AlignTop)


class AttachmentMenu(QMenu):
    """附件按钮的上拉菜单：图片 / 文档 / 文件夹 / 其他会话片段。

    只外发意图；文件选择器与会话片段选择由上层做。
    """

    images_requested = Signal()
    documents_requested = Signal()
    folder_requested = Signal()
    snippet_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 圆角外需透明；必须在首次创建原生窗口前启用，不能依赖绘制时补设。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            f"QMenu {{ padding: 4px; border-radius: {theme.RADIUS_MD}px; }}"
            "QMenu::item { padding: 6px 18px 6px 12px; }"
            f"QMenu::item:selected {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        self.addAction("图片", self.images_requested.emit)
        self.addAction("文档", self.documents_requested.emit)
        self.addAction("文件夹", self.folder_requested.emit)
        self.addSeparator()
        self.addAction("其他会话片段", self.snippet_requested.emit)
        self._anchor: QWidget | None = None
        self._motion = PopupMotion(Qt.UIEffect.UI_AnimateMenu, self)
        install_popup_material(self, radius=theme.RADIUS_MD)

    def popup_above(self, anchor: QWidget) -> None:
        """贴着 ``anchor`` 上方展开（上拉），不阻塞事件循环。"""
        self._anchor = anchor
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        # 先套样式再量尺寸，否则首次弹出的高度偏小、位置会跳
        self.ensurePolished()
        self.adjustSize()
        origin = anchor.mapToGlobal(QPoint(0, 0))
        target = QPoint(origin.x(), origin.y() - self.sizeHint().height() - 4)
        # Windows 的菜单动画固定自上而下卷出，上拉时看起来像先往下闪一下；
        # 只在这次弹出时关掉，其他菜单不受影响；定好位置后再按实际方向补播
        with self._motion.native_effect_suppressed() as animated:
            self.popup(target)
        if animated:
            self._motion.reveal(self, upward=self.y() < origin.y())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 点在触发按钮上时只收起：不把这次按下转交给按钮，否则它会紧接着再弹出一次
        anchor = self._anchor
        if (
            anchor is not None
            and not self.rect().contains(event.position().toPoint())
            and anchor.rect().contains(anchor.mapFromGlobal(event.globalPosition().toPoint()))
        ):
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        super().mousePressEvent(event)

    def setVisible(self, visible: bool) -> None:
        # 菜单的各种关闭路径（点选、Esc、点外面）最终都走到这里；趁它还可见时留下残影
        if not visible and self.isVisible():
            self._motion.collapse()
        super().setVisible(visible)


class AttachmentBar(QWidget):
    """附件栏：chips 流式排列 + 添加按钮。无附件时整体隐藏。"""

    attach_files_requested = Signal()  # 打开文件选择器（由上层做）
    attach_clipboard_requested = Signal()  # 从剪贴板保存（由上层做）
    # 上拉菜单的四类来源（选择器由上层做）
    attach_images_requested = Signal()
    attach_documents_requested = Signal()
    attach_folder_requested = Signal()
    attach_snippet_requested = Signal()
    attachment_removed = Signal(str)  # attachment_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chips: dict[str, _AttachmentChip] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._chips_host = QWidget()
        self._chips_host.setObjectName("attachmentChipsHost")
        self._chips_host.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._chips_layout = QHBoxLayout(self._chips_host)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(8)
        self._chips_layout.addStretch(1)

        # Chips keep their natural width so metadata (including the token
        # estimate) remains readable. Overflow belongs to this strip, never to
        # the composer or the window.
        self._chips_scroll = QScrollArea()
        self._chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chips_scroll.setWidgetResizable(False)
        self._chips_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chips_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._chips_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QWidget#attachmentChipsHost { background: transparent; }"
        )
        self._chips_scroll.setWidget(self._chips_host)
        self._chips_scroll.horizontalScrollBar().rangeChanged.connect(self._sync_scroll_height)
        root.addWidget(self._chips_scroll)

        # 添加按钮行
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        add_files_btn = QPushButton("📎 添加附件")
        add_files_btn.setProperty("flat", True)
        add_files_btn.hide()
        add_files_btn.clicked.connect(self.attach_files_requested.emit)
        add_row.addWidget(add_files_btn)
        add_clipboard_btn = QPushButton("📋 粘贴为文档")
        add_clipboard_btn.setProperty("flat", True)
        add_clipboard_btn.hide()
        add_clipboard_btn.clicked.connect(self.attach_clipboard_requested.emit)
        add_row.addWidget(add_clipboard_btn)
        add_row.addStretch(1)
        root.addLayout(add_row)

        self.setVisible(False)  # 无附件时整体隐藏

    # ---------- 展示更新（由上层驱动） ----------

    def add_attachment(
        self,
        attachment_id: str,
        title: str,
        subtitle: str,
        *,
        thumbnail: QPixmap | None = None,
    ) -> None:
        """加一个附件 chip。"""
        if attachment_id in self._chips:
            return
        chip = _AttachmentChip(attachment_id, title, subtitle, thumbnail=thumbnail)
        chip.remove_clicked.connect(self._on_remove)
        self._chips[attachment_id] = chip
        # 在末尾 stretch 之前插入
        self._chips_layout.insertWidget(self._chips_layout.count() - 1, chip)
        self._resize_chips_host()
        self.setVisible(True)

    def remove_attachment(self, attachment_id: str) -> None:
        chip = self._chips.pop(attachment_id, None)
        if chip is not None:
            self._chips_layout.removeWidget(chip)
            chip.deleteLater()
            self._resize_chips_host()
        if not self._chips:
            self.setVisible(False)

    def clear(self) -> None:
        """发送后清空。"""
        for attachment_id in list(self._chips):
            self.remove_attachment(attachment_id)

    def attachment_ids(self) -> list[str]:
        return list(self._chips)

    def _on_remove(self, attachment_id: str) -> None:
        self.remove_attachment(attachment_id)
        self.attachment_removed.emit(attachment_id)

    def _resize_chips_host(self) -> None:
        """Keep the scroll content at its natural width after add/remove."""
        chips = list(self._chips.values())
        for chip in chips:
            chip.ensurePolished()
        spacing = self._chips_layout.spacing()
        width = sum(chip.sizeHint().width() for chip in chips)
        width += max(0, len(chips) - 1) * spacing
        height = max((chip.sizeHint().height() for chip in chips), default=0)
        self._chips_host.setFixedSize(width, height)
        self._chips_layout.activate()
        self._sync_scroll_height()

    def _sync_scroll_height(self, minimum: int = 0, maximum: int = 0) -> None:
        """Reserve vertical room for the chip row and an overflow scrollbar."""
        del minimum
        content_height = self._chips_host.height()
        if maximum > 0 or self._chips_scroll.horizontalScrollBar().maximum() > 0:
            content_height += self._chips_scroll.horizontalScrollBar().sizeHint().height()
        self._chips_scroll.setFixedHeight(content_height)
