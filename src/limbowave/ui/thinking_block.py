"""Selectable reasoning: no hidden layout work, bounded-frequency incremental display."""

from __future__ import annotations

import math

from PySide6.QtCore import QSizeF, Qt, QTimer
from PySide6.QtGui import QHideEvent, QShowEvent, QTextCursor
from PySide6.QtWidgets import QFrame, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from limbowave.ui import theme


class ThinkingBlock(QWidget):
    """Keep all reasoning, but only materialize its document when expanded.

    A separate cursor appends plain text without replacing the document or the
    user's selection. A single-shot timer coalesces bursts (not a debounce, so
    continuous output cannot starve the display). All state lives on the GUI thread.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chunks: list[str] = []
        self._rendered_chunks = 0
        self._has_content = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._toggle = QPushButton("▸ 思考过程")
        self._toggle.setStyleSheet(
            f"QPushButton {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
            f" padding: 2px 8px 2px 0; border: none; border-radius: {theme.RADIUS_SM}px;"
            " background: transparent; }"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self._content = QTextBrowser()
        self._content.setFrameShape(QFrame.Shape.NoFrame)
        self._content.setUndoRedoEnabled(False)
        self._content.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content.document().setDocumentMargin(0)
        self._content.setStyleSheet(
            f"QTextBrowser {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
            f" background: transparent; border: none; border-left: 2px solid {theme.BORDER};"
            " padding: 2px 0 2px 12px; }"
            "QTextBrowser QScrollBar { width: 0px; height: 0px; }"
        )
        self._content.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self._content.hide()
        layout.addWidget(self._content)

    def text(self) -> str:
        """Full buffered text, independent of whether the document is displayed."""
        return "".join(self._chunks)

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._chunks.append(delta)
        self._has_content = self._has_content or bool(delta.strip())
        if self._content.isVisible() and not self._flush_timer.isActive():
            self._flush_timer.start()

    def set_text(self, text: str) -> None:
        self._flush_timer.stop()
        self._chunks = [text] if text else []
        self._has_content = bool(text.strip())
        self._rendered_chunks = 0
        if not self._content.document().isEmpty():
            self._content.clear()
        if not self._content.isHidden():
            self._flush()

    def has_content(self) -> bool:
        return self._has_content

    def _flush(self) -> None:
        self._flush_timer.stop()
        if self._content.isHidden() or self._rendered_chunks == len(self._chunks):
            return
        delta = "".join(self._chunks[self._rendered_chunks:])
        self._rendered_chunks = len(self._chunks)
        cursor = QTextCursor(self._content.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)

    def _fit_height(self, size: QSizeF) -> None:
        height = max(1, math.ceil(size.height()) + 4)  # CSS top/bottom padding
        if self._content.height() == height:
            return
        self._content.setFixedHeight(height)
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
        self.updateGeometry()

    def _on_toggle(self) -> None:
        # isVisible() also reflects hidden ancestors, not the user's expansion state.
        expanded = self._content.isHidden()
        self._content.setVisible(expanded)
        self._toggle.setText(("▾" if expanded else "▸") + " 思考过程")
        if expanded:
            self._flush()
        else:
            self._flush_timer.stop()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._flush()

    def hideEvent(self, event: QHideEvent) -> None:
        self._flush_timer.stop()
        super().hideEvent(event)
