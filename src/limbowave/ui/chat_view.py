"""聊天视图：消息列表 + 输入区 + 状态栏。

架构约束：本视图持有**展示态**（当前消息流的呈现），这是视图职责所在；
权威业务状态（会话树、分支、权限、上下文）不在此处。用户意图经信号外发。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class ChatView(QWidget):
    """最小聊天视图。通过信号表达意图，经方法接收展示更新。"""

    message_submitted = Signal(str)
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stream_label: QLabel | None = None
        self._build()

    # ---------- 布局 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 消息区
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._transcript_host = QWidget()
        self._transcript = QVBoxLayout(self._transcript_host)
        self._transcript.setContentsMargins(16, 16, 16, 16)
        self._transcript.setSpacing(10)
        self._transcript.addStretch(1)
        self._scroll.setWidget(self._transcript_host)
        root.addWidget(self._scroll, 1)

        # 状态栏
        self._status = QLabel("就绪")
        self._status.setContentsMargins(16, 4, 16, 4)
        self._status.setStyleSheet("color: #888; font-size: 12px;")
        root.addWidget(self._status)

        # 输入区
        bottom = QHBoxLayout()
        bottom.setContentsMargins(16, 8, 16, 12)
        bottom.setSpacing(8)
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("输入消息…（Ctrl+Enter 发送）")
        self._input.setMaximumHeight(96)
        bottom.addWidget(self._input, 1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setDefault(True)
        self._send_btn.clicked.connect(self._on_send)
        bottom.addWidget(self._send_btn)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        bottom.addWidget(self._stop_btn)
        root.addLayout(bottom)

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self.message_submitted.emit(text)

    # ---------- 展示更新（由控制器驱动）----------

    def set_available(self, available: bool, hint: str = "") -> None:
        self._send_btn.setEnabled(available)
        self._input.setEnabled(available)
        if available:
            self.set_status("就绪")
        else:
            self.set_status(hint or "未配置可用的模型内核")

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_busy(self, busy: bool) -> None:
        self._stop_btn.setEnabled(busy)
        self._send_btn.setEnabled(not busy)

    def add_user_message(self, text: str) -> None:
        self._add_bubble(text, role="user")
        self._scroll_to_bottom()

    def begin_assistant(self) -> None:
        self._stream_label = self._add_bubble("", role="assistant")
        self._scroll_to_bottom()

    def append_assistant_delta(self, text: str) -> None:
        if self._stream_label is None:
            self.begin_assistant()
        assert self._stream_label is not None
        self._stream_label.setText(self._stream_label.text() + text)
        self._scroll_to_bottom()

    def end_assistant(self, text: str) -> None:
        if self._stream_label is None:
            self._add_bubble(text, role="assistant")
        else:
            self._stream_label.setText(text)
        self._stream_label = None
        self._scroll_to_bottom()

    def add_error(self, message: str) -> None:
        self._add_bubble(f"⚠ {message}", role="error")
        self._scroll_to_bottom()

    # ---------- 内部 ----------

    def _add_bubble(self, text: str, *, role: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setContentsMargins(10, 8, 10, 8)
        styles = {
            "user": "background:#2d4a6b; color:#fff; border-radius:8px;",
            "assistant": "background:#2b2b2b; color:#eee; border-radius:8px;",
            "error": "background:#5b2b2b; color:#ffd7d7; border-radius:8px;",
        }
        label.setStyleSheet(styles.get(role, styles["assistant"]))
        # 在 stretch 之前插入
        self._transcript.insertWidget(self._transcript.count() - 1, label)
        return label

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
