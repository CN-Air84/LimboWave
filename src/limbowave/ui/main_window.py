"""主窗口：只负责展示状态与发出命令，不持有权威业务状态。

架构约束（见设计计划 §二.1）：会话树、路由、权限、上下文一律留在应用核心。
本窗口通过信号向外表达用户意图，不自己保存或改写业务数据。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMainWindow, QWidget

from limbowave.bootstrap import APP_DISPLAY_NAME
from limbowave.ui.chat_view import ChatView


class MainWindow(QMainWindow):
    """主窗口：承载聊天视图，把用户意图以信号形式外发。"""

    # 用户意图出口。上层控制器负责解释命令，窗口不关心它意味着什么。
    command_requested = Signal(str)
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1200, 800)
        self._chat_view = ChatView()
        self._chat_view.message_submitted.connect(self.command_requested)
        self._chat_view.stop_requested.connect(self.stop_requested)
        self.setCentralWidget(self._chat_view)

    @property
    def chat(self) -> ChatView:
        """聊天视图（供上层接线）。注意这是访问器，不在实例字典里。"""
        return self._chat_view
