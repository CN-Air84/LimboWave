"""设置数据页的重置确认面板；验证与删除由应用层接线。"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget

from limbowave.application.services.data_reset_service import RESET_WAIT_SECONDS, ResetScope
from limbowave.domain.platform_capabilities import (
    DATA_RESET_UNAVAILABLE,
    supports_system_identity,
)
from limbowave.ui import theme
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.login_page import _AnimatedPasswordEdit


class DataResetPanel(FloatingPanel):
    verification_requested = Signal(str, object)  # 主密码、ResetScope；不读取系统 PIN。
    confirmation_requested = Signal()

    def __init__(self, parent: QWidget, data_root: str) -> None:
        super().__init__(parent, "重置数据", width=520)
        self._identity_supported = supports_system_identity()
        self._verifying = False
        self._deadline: float | None = None
        self._submitted = False
        self._scope = QComboBox()
        self._scope.addItem("只删除数据库", ResetScope.DATABASE.value)
        self._scope.addItem("完全重置", ResetScope.ALL.value)
        self.content_layout.addWidget(self._scope)
        self._description = QLabel()
        self._description.setWordWrap(True)
        self.content_layout.addWidget(self._description)
        location = QLabel(f"数据目录：{data_root}")
        location.setWordWrap(True)
        location.setTextFormat(Qt.TextFormat.PlainText)
        self.content_layout.addWidget(location)
        warning = QLabel("直接删除，不经过回收站，无法撤销；不自动备份。确认后应用退出。")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.DANGER_TEXT};")
        self.content_layout.addWidget(warning)
        self._password = _AnimatedPasswordEdit()
        self._password.setPlaceholderText("重新输入主密码")
        self.content_layout.addWidget(self._password)
        self._verify = QPushButton("验证主密码和 Windows Hello PIN")
        self._verify.setProperty("accent", True)
        self._verify.clicked.connect(self._request_verification)
        self._password.returnPressed.connect(self._request_verification)
        self.content_layout.addWidget(self._verify)
        self._status = QLabel("PIN 仅在 Windows 系统窗口中输入，应用不会读取或保存。")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self.content_layout.addWidget(self._status)
        row = QHBoxLayout()
        row.addStretch(1)
        self._cancel = QPushButton("取消")
        self._cancel.clicked.connect(self.close_panel)
        row.addWidget(self._cancel)
        self._confirm = QPushButton("立即删除并退出")
        self._confirm.setEnabled(False)
        self._confirm.setStyleSheet(
            f"QPushButton:enabled {{ background: {theme.DANGER_BG}; color: {theme.DANGER_TEXT}; }}"
        )
        self._confirm.clicked.connect(self._request_confirmation)
        row.addWidget(self._confirm)
        self.content_layout.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._update_countdown)
        self.closed.connect(self._on_closed)
        self._scope.currentIndexChanged.connect(self._describe_scope)
        self._describe_scope()
        if not self._identity_supported:
            self._verify.setEnabled(False)
            self._password.setEnabled(False)
            self._scope.setEnabled(False)
            self._status.setText(DATA_RESET_UNAVAILABLE)

    def _describe_scope(self) -> None:
        if self._scope.currentData() == ResetScope.ALL.value:
            text = (
                "删除数据库、主密码、API 密钥、应用内附件、模型配置、外观设置与运行缓存。"
                "下次启动重新设置主密码。用户备份、导出及原始文件保留。"
            )
        else:
            text = (
                "删除会话、消息、请求日志、授权记录及附件索引所在的数据库和 SQLite 辅助文件。"
                "主密码、API 密钥、设置、磁盘附件和备份保留。"
            )
        self._description.setText(text)

    def _request_verification(self) -> None:
        if not self._identity_supported:
            return
        if self._closing or self._verifying or self._deadline is not None:
            return
        password = self._password.text()  # 主密码允许首尾空格，不能 strip。
        if not password:
            self._status.setText("请输入主密码。")
            return
        self._verifying = True
        self._scope.setEnabled(False)
        self._password.clear()
        self._password.setEnabled(False)
        self._verify.setEnabled(False)
        self._status.setText("正在验证主密码；随后请在 Windows Hello 系统窗口中输入 PIN…")
        self.verification_requested.emit(password, ResetScope(self._scope.currentData()))

    def verification_failed(self, detail: str) -> None:
        if not self._identity_supported:
            return
        if self._closing:
            return
        self._verifying = False
        self._scope.setEnabled(True)
        self._password.setEnabled(True)
        self._verify.setEnabled(True)
        self._status.setText(detail)
        self._password.setFocus()

    def verification_succeeded(self) -> None:
        if not self._identity_supported:
            return
        if self._closing:
            return
        self._verifying = False
        self._deadline = time.monotonic() + RESET_WAIT_SECONDS
        self._status.setText("主密码和 Windows Hello 验证通过。请核对删除范围，等待后再次确认。")
        self._cancel.setFocus()
        self._update_countdown()
        self._timer.start()

    def _update_countdown(self) -> None:
        if self._deadline is None or self._closing or self._submitted:
            return
        remaining = max(0, math.ceil(self._deadline - time.monotonic()))
        self._confirm.setText(f"请等待 {remaining} 秒" if remaining else "立即删除并退出")
        self._confirm.setEnabled(remaining == 0)
        if remaining == 0:
            self._timer.stop()

    def _request_confirmation(self) -> None:
        if (
            self._closing or self._submitted or self._deadline is None
            or time.monotonic() < self._deadline
        ):
            return
        self._submitted = True
        self._confirm.setEnabled(False)
        self.confirmation_requested.emit()

    def _on_closed(self) -> None:
        self._timer.stop()
        self._password.clear()
