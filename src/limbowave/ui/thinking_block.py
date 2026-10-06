"""Selectable reasoning: no hidden layout work, bounded-frequency incremental display."""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QSizeF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QHideEvent, QShowEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme

_REVEAL_MS = 200
_CONTENT_GAP = 6


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
        self._expanded = False
        self._reveal_progress = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(_REVEAL_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._set_reveal_progress)
        self._animation.finished.connect(self._settle_reveal)
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._toggle = QPushButton("▸ 思考过程")
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet(
            f"QPushButton {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
            f" padding: 2px 8px 2px 0; border: none; border-radius: {theme.RADIUS_SM}px;"
            " background: transparent; }"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)

        # Clip a full-height document rather than shrinking its viewport each frame.
        # The top gap belongs to the reveal, so neither endpoint jumps by 6px.
        self._clip = QWidget()
        self._clip.setFixedHeight(0)
        self._clip.hide()
        layout.addWidget(self._clip)
        self._content = QTextBrowser(self._clip)
        self._content.move(0, _CONTENT_GAP)
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
        self._clip.installEventFilter(self)

    def text(self) -> str:
        """Full buffered text, independent of whether the document is displayed."""
        return "".join(self._chunks)

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._chunks.append(delta)
        self._has_content = self._has_content or bool(delta.strip())
        if self._expanded and self._content.isVisible() and not self._flush_timer.isActive():
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
        if (
            not self._expanded
            or self._content.isHidden()
            or self._rendered_chunks == len(self._chunks)
        ):
            return
        delta = "".join(self._chunks[self._rendered_chunks:])
        self._rendered_chunks = len(self._chunks)
        cursor = QTextCursor(self._content.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)

    def _fit_height(self, size: QSizeF) -> None:
        height = max(1, math.ceil(size.height()) + 4)  # CSS top/bottom padding
        if self._content.height() != height:
            self._content.setFixedHeight(height)
        self._sync_reveal_height()

    def _sync_reveal_height(self) -> None:
        height = round((self._content.height() + _CONTENT_GAP) * self._reveal_progress)
        if self._clip.height() != height:
            self._clip.setFixedHeight(height)

    def _set_reveal_progress(self, progress: float) -> None:
        self._reveal_progress = progress
        self._sync_reveal_height()

    def _settle_reveal(self) -> None:
        self._animation.stop()
        self._set_reveal_progress(1.0 if self._expanded else 0.0)
        self._content.setVisible(self._expanded)
        self._clip.setVisible(self._expanded)

    def _on_toggle(self) -> None:
        # Visibility is not expansion state: closing content stays visible until
        # the animation finishes, and ancestors may be hidden during a session switch.
        self._expanded = not self._expanded
        self._toggle.setChecked(self._expanded)
        self._toggle.setText(("▾" if self._expanded else "▸") + " 思考过程")
        if self._expanded:
            self._clip.show()
            layout = self.layout()
            if layout is not None:
                layout.activate()
            self._content.setFixedWidth(self._clip.width())
            self._content.show()
            self._flush()
        else:
            self._flush_timer.stop()
        if not self.isVisible() or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General):
            self._settle_reveal()
            return
        # Reverse the same easing timeline on repeated clicks without restarting.
        self._animation.setDirection(
            QAbstractAnimation.Direction.Forward
            if self._expanded else QAbstractAnimation.Direction.Backward
        )
        if self._animation.state() == QAbstractAnimation.State.Stopped:
            self._animation.start()
        elif self._animation.state() == QAbstractAnimation.State.Paused:
            self._animation.resume()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._clip and event.type() == QEvent.Type.Resize:
            width = self._clip.width()
            if self._content.width() != width:
                self._content.setFixedWidth(width)
        return super().eventFilter(watched, event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._flush()

    def hideEvent(self, event: QHideEvent) -> None:
        self._flush_timer.stop()
        self._settle_reveal()
        super().hideEvent(event)
