"""Compact per-conversation queue with global order and explicit resume/cancel actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.send_queue import QueuedSend
from limbowave.ui import theme


class SendQueuePanel(QWidget):
    cancel_requested = Signal(str)
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sendQueuePanel")
        self._signature: object = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        header = QHBoxLayout()
        self._summary = QLabel()
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        self._summary.setWordWrap(True)
        header.addWidget(self._summary, 1)
        self._resume = QPushButton("继续队列")
        self._resume.setProperty("flat", True)
        self._resume.clicked.connect(self.resume_requested.emit)
        header.addWidget(self._resume)
        root.addLayout(header)
        self._list = QWidget()
        self._list.setObjectName("sendQueueEntries")
        self._rows = QVBoxLayout(self._list)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(4)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMaximumHeight(116)
        self._scroll.setWidget(self._list)
        self._scroll.setStyleSheet("QScrollArea, QWidget { background: transparent; }")
        root.addWidget(self._scroll)
        self.setStyleSheet(
            "QWidget#sendQueuePanel, QWidget#sendQueueEntries { background: transparent; }"
            f"QLabel {{ background: transparent; color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FS_SMALL}px; }}"
        )
        self.hide()

    def update_queue(
        self, entries: tuple[tuple[int, QueuedSend], ...], total: int, paused_reason: str,
        *, dispatching: bool = False,
    ) -> None:
        signature = (entries, total, paused_reason, dispatching)
        if self._signature == signature:
            return
        self._signature = signature
        self.setVisible(total > 0)
        self._summary.setText(
            f"发送队列 · 共 {total} 条" + (f" · 已暂停：{paused_reason}" if paused_reason else "")
        )
        self._resume.setVisible(bool(paused_reason))
        self._resume.setEnabled(not dispatching)
        while self._rows.count():
            child = self._rows.takeAt(0)
            if child is not None and (widget := child.widget()) is not None:
                widget.setParent(None)
                widget.deleteLater()
        for position, item in entries:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            state = {"waiting": "等待中", "sending": "正在发送", "failed": "发送失败"}[item.state]
            snippet = " ".join(item.text.split())
            excerpt = snippet[:55] + ("…" if len(snippet) > 55 else "")
            label = QLabel(f"{position}. {state} · {excerpt}")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setToolTip(item.text + (f"\n{item.error}" if item.error else ""))
            layout.addWidget(label, 1)
            cancel = QPushButton("撤回草稿")
            cancel.setObjectName(f"cancel_queue_{item.id}")
            cancel.setProperty("flat", True)
            cancel.setToolTip("取消排队，并把文字放回所属会话的草稿")
            cancel.setEnabled(item.state != "sending")
            cancel.clicked.connect(
                lambda _checked=False, key=item.id: self.cancel_requested.emit(key)
            )
            layout.addWidget(cancel)
            self._rows.addWidget(row)
        self._scroll.setVisible(bool(entries))
        self._scroll.setMinimumHeight(min(116, max(36, len(entries) * 40)))
