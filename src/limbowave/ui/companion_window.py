"""Small, presentation-only companion surface; no conversation/runtime state."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QKeySequence,
    QResizeEvent,
    QShowEvent,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from limbowave.domain.files import SUPPORTED_ATTACHMENTS_HINT
from limbowave.ui import theme
from limbowave.ui.attachment_bar import AttachmentBar, DraftAttachment


def visible_geometry(rect: QRect, available: QRect) -> QRect:
    """Clamp to a screen work area, including negative-coordinate monitors."""
    width = min(rect.width(), available.width())
    height = min(rect.height(), available.height())
    return QRect(
        max(available.left(), min(rect.x(), available.right() - width + 1)),
        max(available.top(), min(rect.y(), available.bottom() - height + 1)),
        width, height,
    )


class _MiniInput(QPlainTextEdit):
    submitted = Signal()
    attachment_pasted = Signal(object)
    attachments_enabled = False

    @staticmethod
    def _is_attachment(source: QMimeData) -> bool:
        return (any(url.isLocalFile() for url in source.urls())
                or (source.hasImage() and not source.hasText()))

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        if self._is_attachment(source):
            return self.attachments_enabled
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if self._is_attachment(source):
            if self.attachments_enabled:
                self.attachment_pasted.emit(source)
            return
        # Text uses THIS cursor, not the hidden editor's cursor in the shared document.
        super().insertFromMimeData(source)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            if not event.isAutoRepeat():
                self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CompanionWindow(QWidget):
    restore_requested = Signal()
    submit_requested = Signal()
    stop_requested = Signal()
    shortcut_requested = Signal(str)
    global_shortcut_toggled = Signal(bool)
    attach_requested = Signal()
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._controls_ready = False
        self.setObjectName("companionWindow")
        self.setWindowTitle("灵波 · 陪伴")
        self.setMinimumWidth(340)
        self.resize(420, 480)
        self._expanded_size = QSize(420, 480)
        self._collapsed = False
        self._messages: tuple[tuple[str, str], ...] = ()
        self._theme_signature: tuple[object, ...] = ()
        self._idle_document = self._make_idle_document()
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(12)
        # Controls are overlays, not rows reserving opaque header/footer bands.
        self._top_controls = QWidget(self)
        self._top_controls.setObjectName("companionTopControls")
        header = QHBoxLayout(self._top_controls)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._collapse = self._button("收起", "收起为迷你条（Esc）")
        self._collapse.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        header.addWidget(self._collapse)
        self._restore = self._button("主界面", "返回完整对话、附件与权限设置")
        self._restore.clicked.connect(self.restore_requested)
        header.addWidget(self._restore)
        self._options = self._button("...", "陪伴窗口选项")
        self._options.setObjectName("companionOptions")
        self._options.setFixedWidth(32)
        menu = QMenu(self._options)
        self._pin = menu.addAction("窗口置顶")
        self._pin.setCheckable(True)
        self._pin.toggled.connect(self._set_pinned)
        self._translucent = menu.addAction("半透明显示")
        self._translucent.setCheckable(True)
        self._translucent.toggled.connect(
            lambda checked: self.setWindowOpacity(0.90 if checked else 1.0)
        )
        menu.addSeparator()
        self._global_shortcut = menu.addAction("启用全局呼出（Windows）")
        self._global_shortcut.setCheckable(True)
        self._global_shortcut.setChecked(True)
        self._global_shortcut.toggled.connect(self.global_shortcut_toggled)
        self._shortcut_action = menu.addAction("设置呼出快捷键（本次运行）…")
        self._shortcut_action.triggered.connect(self._configure_shortcut)
        self._options.setMenu(menu)
        header.addWidget(self._options)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        transcript = QWidget()
        transcript.setObjectName("companionTranscript")
        transcript_layout = QVBoxLayout(transcript)
        transcript_layout.setContentsMargins(0, 42, 4, 0)
        transcript_layout.setSpacing(10)
        transcript_layout.addStretch(1)
        self._empty = QLabel("我在这里。\n想说点什么，随时都可以。")
        self._empty.setObjectName("companionEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        transcript_layout.addWidget(self._empty)
        self._cards: list[QLabel] = []
        self._message_rows: list[QWidget] = []
        self._message_layouts: list[QHBoxLayout] = []
        for _ in range(3):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            row_layout.addStretch(0)
            card = QLabel()
            card.setObjectName("companionMessage")
            card.setTextFormat(Qt.TextFormat.PlainText)
            card.setWordWrap(True)
            card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row_layout.addWidget(card, 1)
            row.hide()
            transcript_layout.addWidget(row)
            self._cards.append(card)
            self._message_rows.append(row)
            self._message_layouts.append(row_layout)
        self._scroll.setWidget(transcript)
        self._scroll.viewport().installEventFilter(self)
        body.addWidget(self._scroll, 1)
        self._status = QLabel("就绪")
        self._status.setObjectName("companionStatus")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        body.addWidget(self._status)
        self._notice = QLabel()
        self._notice.setObjectName("companionNotice")
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        self._notice.setWordWrap(True)
        self._notice.hide()
        body.addWidget(self._notice)
        self._attachments = AttachmentBar()
        self._attachment_snapshot: tuple[DraftAttachment, ...] = ()
        body.addWidget(self._attachments)
        self._input = _MiniInput()
        self._input.setObjectName("companionInput")
        self._input.setAccessibleName("陪伴模式输入框")
        self._input.setPlaceholderText("说点什么…")
        self._input.setMinimumHeight(100)
        self._input.setMaximumHeight(116)
        self._input.setViewportMargins(0, 0, 0, 34)
        self._input.installEventFilter(self)
        self._input.setAcceptDrops(False)
        self._input.setToolTip("支持粘贴文件或图片，也可拖入附件。" + SUPPORTED_ATTACHMENTS_HINT)
        self._input.submitted.connect(self.submit_requested)
        body.addWidget(self._input)
        self._bottom_controls = QWidget(self._input)
        self._bottom_controls.setObjectName("companionBottomControls")
        footer = QHBoxLayout(self._bottom_controls)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        self._attach = self._button("附件", "添加附件")
        self._attach.setObjectName("companionAttach")
        self._attach.setToolTip(SUPPORTED_ATTACHMENTS_HINT)
        self._attach.clicked.connect(self.attach_requested)
        footer.addWidget(self._attach)
        footer.addStretch(1)
        self._stop = self._button("停止", "停止当前生成")
        self._stop.clicked.connect(self.stop_requested)
        footer.addWidget(self._stop)
        self._send = self._button("发送", "发送（Ctrl+Enter）")
        self._send.setObjectName("companionSend")
        self._send.clicked.connect(self.submit_requested)
        footer.addWidget(self._send)
        root.addWidget(self._body, 1)
        self._shortcut = "Ctrl+Alt+Space"
        self._shortcut_status = ""
        self._controls_ready = True
        self.restyle()
        self._place_controls()

    def _place_controls(self) -> None:
        self._top_controls.adjustSize()
        self._top_controls.move(max(8, self.width() - self._top_controls.width() - 12), 10)
        self._top_controls.raise_()
        self._bottom_controls.setGeometry(8, self._input.height() - 36,
                                          max(0, self._input.width() - 16), 30)
        self._bottom_controls.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._controls_ready:
            self._place_controls()

    def _make_idle_document(self) -> QTextDocument:
        # QPlainTextEdit creates the required QPlainTextDocumentLayout.
        holder = QPlainTextEdit(self)
        document = holder.document()
        document.setParent(self)
        holder.deleteLater()
        return document

    @staticmethod
    def _button(text: str, accessible: str) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(accessible)
        button.setToolTip(accessible)
        button.setMinimumHeight(30)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @property
    def messages(self) -> tuple[tuple[str, str], ...]:
        return self._messages

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def bind_document(self, document: QTextDocument | None) -> None:
        target = document if document is not None else self._idle_document
        if self._input.document() is not target:
            self._input.setDocument(target)

    def show_messages(self, messages: tuple[tuple[str, str], ...]) -> None:
        if messages == self._messages:
            return
        self._messages = messages
        scroll = self._scroll.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 24
        self._empty.setVisible(not messages)
        for index, card in enumerate(self._cards):
            self._message_rows[index].setVisible(index < len(messages))
            if index < len(messages):
                role, text = messages[index]
                user = role == "user"
                name = "companionUserBubble" if user else "companionMessage"
                if card.objectName() != name:
                    card.setObjectName(name)
                    card.style().unpolish(card)
                    card.style().polish(card)
                row_layout = self._message_layouts[index]
                row_layout.setStretch(0, 1 if user else 0)
                row_layout.setStretch(1, 0 if user else 1)
                card.setText(text or ("正在回应…" if role == "assistant" else ""))
            else:
                card.clear()
        self._fit_bubbles()
        if at_bottom:
            # Let layout settle before scrolling; never activate or focus on a delta.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: scroll.setValue(scroll.maximum()))

    def _fit_bubbles(self) -> None:
        maximum = max(1, round(self._scroll.viewport().width() * 0.8))
        for card in self._cards:
            if card.objectName() == "companionUserBubble":
                text_width = max((card.fontMetrics().horizontalAdvance(line)
                                  for line in card.text().splitlines()), default=0)
                card.setFixedWidth(min(maximum, text_width + 28))
            else:
                card.setMinimumWidth(0)
                card.setMaximumWidth(16777215)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Application QSS sends style/layout events while children are being
        # constructed; no sibling references are safe until construction ends.
        if not self._controls_ready or event.type() != QEvent.Type.Resize:
            return super().eventFilter(watched, event)
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_bubbles()
        if watched is self._input and event.type() == QEvent.Type.Resize:
            self._place_controls()
        return super().eventFilter(watched, event)

    def show_attachments(self, items: tuple[DraftAttachment, ...]) -> None:
        if items != self._attachment_snapshot:
            self._attachment_snapshot = items
            self._attachments.restore(items)

    def set_attachments_enabled(self, enabled: bool) -> None:
        self._attach.setEnabled(enabled)
        self._input.attachments_enabled = enabled
        self.setAcceptDrops(enabled)

    def _accept_files(self, event: QDropEvent) -> bool:
        return (self.acceptDrops()
                and any(url.isLocalFile() for url in event.mimeData().urls()))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accept_files(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._accept_files(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if self._accept_files(event):
            self.files_dropped.emit([url.toLocalFile() for url in event.mimeData().urls()
                                     if url.isLocalFile()])
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def clear_projection(self) -> None:
        self.bind_document(None)
        self.show_messages(())
        self.show_attachments(())
        self.set_attachments_enabled(False)
        self._notice.clear()
        self._status.setText("就绪")

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._collapse.setText("展开" if collapsed else "收起")
        self._collapse.setAccessibleName("展开陪伴窗口" if collapsed else "收起为迷你条（Esc）")
        if collapsed:
            self._expanded_size = self.size()
            self.setFixedHeight(54)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.resize(self._expanded_size)
            self._input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._place_controls()
        self.ensure_on_screen()

    def ensure_on_screen(self) -> None:
        from PySide6.QtWidgets import QApplication
        screens = QApplication.screens()
        if not screens:
            return
        # Choose by overlap, falling back to the primary monitor after removal.
        screen = max(screens, key=lambda s: self.frameGeometry().intersected(
            s.availableGeometry()).width() * max(0, self.frameGeometry().intersected(
                s.availableGeometry()).height()))
        available = screen.availableGeometry()
        frame_margin = self.frameGeometry().width() - self.width()
        self.setMinimumWidth(min(340, max(1, available.width() - frame_margin)))
        if not self.collapsed and available.height() < self.minimumSizeHint().height() + 40:
            self.set_collapsed(True)
            return
        frame = self.frameGeometry()
        target = visible_geometry(frame, available)
        if target != frame:
            extra = frame.size() - self.size()
            self.resize(max(1, target.width() - extra.width()),
                        max(1, target.height() - extra.height()))
            # QWidget.move positions the native frame, not its client rect.
            self.move(target.topLeft())

    def _set_pinned(self, pinned: bool) -> None:
        geometry = self.geometry()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        self.setGeometry(geometry)
        self.show()
        self.ensure_on_screen()

    def set_shortcut_status(self, sequence: str, status: str) -> None:
        self._shortcut = sequence
        self._shortcut_status = status
        self._options.setToolTip(status)

    def _configure_shortcut(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("呼出快捷键 · 本次运行")
        layout = QVBoxLayout(dialog)
        label = QLabel(self._shortcut_status + "\n应用内始终可用 Ctrl+Shift+M。")
        label.setWordWrap(True)
        layout.addWidget(label)
        editor = QKeySequenceEdit(QKeySequence(self._shortcut))
        editor.setMaximumSequenceLength(1)
        editor.setAccessibleName("全局呼出快捷键")
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.shortcut_requested.emit(editor.keySequence().toString(
                QKeySequence.SequenceFormat.PortableText))
        dialog.deleteLater()

    def restyle(self) -> None:
        signature = (theme.BG_APP, theme.BG_SURFACE, theme.TEXT_PRIMARY, theme.ACCENT,
                     theme.TEXT_SECONDARY, theme.BORDER, theme.FS_BASE, theme.user_bubble_surface())
        if signature == self._theme_signature:
            return
        self._theme_signature = signature
        self.setStyleSheet(f"""
            QWidget#companionWindow {{ background: {theme.BG_APP}; }}
            QLabel {{ color: {theme.TEXT_PRIMARY};
                background: transparent; font-size: {theme.FS_BASE}px; }}
            QWidget#companionTopControls, QWidget#companionBottomControls
                {{ background: transparent; border: none; }}
            QLabel#companionEmpty {{ color: {theme.TEXT_SECONDARY}; padding: 26px 8px; }}
            QLabel#companionMessage {{ background: {theme.BG_SURFACE};
                border: 1px solid {theme.BORDER}; border-radius: 12px; padding: 12px; }}
            QLabel#companionUserBubble {{ {theme.bubble_stylesheet("user")}
                border: none; padding: 10px 14px; }}
            QLabel#companionStatus {{ color: {theme.TEXT_SECONDARY};
                font-size: {theme.FS_SMALL}px; }}
            QLabel#companionNotice {{ color: {theme.WARNING}; font-size: {theme.FS_SMALL}px; }}
            QScrollArea, QWidget#companionTranscript {{ background: transparent; }}
            QPlainTextEdit#companionInput {{ background: {theme.BG_SURFACE};
                color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER};
                border-radius: 12px; padding: 9px; font-size: {theme.FS_BASE}px; }}
            QPlainTextEdit#companionInput:focus {{ border-color: {theme.ACCENT}; }}
            QPushButton {{ color: {theme.TEXT_SECONDARY};
                background: transparent; border: 1px solid transparent;
                border-radius: 8px; padding: 3px 8px; font-size: {theme.FS_SMALL}px; }}
            QPushButton:hover {{ background: {theme.BG_SURFACE_HOVER};
                color: {theme.TEXT_PRIMARY}; }}
            QPushButton:focus {{ border-color: {theme.ACCENT}; }}
            QPushButton#companionOptions {{ padding: 0; font-size: 20px; }}
            QPushButton#companionOptions::menu-indicator {{ image: none; width: 0; }}
            QPushButton#companionSend {{ background: {theme.ACCENT};
                color: {theme.TEXT_ON_ACCENT}; }}
            QPushButton#companionSend:disabled {{ background: {theme.BG_ELEVATED};
                color: {theme.TEXT_SECONDARY}; }}
        """)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.set_collapsed(True)
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.ensure_on_screen()
        self._place_controls()

    def closeEvent(self, event: QCloseEvent) -> None:
        # X/Alt+F4 closes this view, never the app or its running request.
        event.ignore()
        self.restore_requested.emit()
