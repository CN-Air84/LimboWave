"""上下文压缩的 UI 组件（Phase 5）。

- ``ContextUsageBar``：占用估算条。展示为「约 N%（估算）」——不伪装精确值。
  达到阈值变色：70% 提示色、80% 警告色、90% 阻止色。
- ``CompressButton``：**长按确认**的压缩按钮（设计计划 §三.1：长按后弹出确认，
  减少误触）。短按无效，长按到阈值才发出 ``confirmed``。
- ``CompressionPreviewDialog``：压缩预览。查看摘要、编辑、接受、重试、拒绝、
  回退到旧版本。失败时如实显示错误，不伪造摘要。
- ``CompressionThinkingPanel``：压缩期间的思考过程悬浮窗。压缩模型的输出不是
  对话的一轮，思考流不进消息区气泡，在这里单独展示（只读、不落库）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.compression_service import (
    CompressionService,
)
from limbowave.domain.compaction import CompressionVersion
from limbowave.ui import theme
from limbowave.ui.floating import FloatingPanel


class ContextUsageBar(QWidget):
    """上下文占用估算条。读数来自内核估算，展示为估算。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.card_surface()}; border: none;"
            f" border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 3px; }}"
        )
        layout.addWidget(self._bar, 1)

        self._label = QLabel("未知")
        self._label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
        )
        layout.addWidget(self._label)

    def set_usage(self, percent: float | None, display: str) -> None:
        """更新占用。percent 为 None 表示内核不支持估算。"""
        if percent is None:
            self._bar.setValue(0)
            self._label.setText(display)
            return
        value = max(0, min(100, int(percent)))
        self._bar.setValue(value)
        self._label.setText(display)
        # 阈值变色（与服务层 threshold_action 同档）
        if percent >= 90:
            color = theme.DANGER_TEXT
        elif percent >= 80:
            color = theme.WARNING
        elif percent >= 70:
            color = theme.TEXT_SECONDARY
        else:
            color = theme.ACCENT
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.card_surface()}; border: none;"
            f" border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )


class CompressButton(QPushButton):
    """长按确认的压缩按钮。长按 ``HOLD_MS`` 毫秒才发出 ``confirmed``。"""

    HOLD_MS = 600

    confirmed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("压缩上下文", parent)
        self.setToolTip("长按压缩上下文（松开取消）")
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self.HOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold_complete)

    # 长按手势：按下启动计时，提前松开取消，到点才确认
    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._hold_timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._hold_timer.stop()
        super().mouseReleaseEvent(event)

    def _on_hold_complete(self) -> None:
        self.confirmed.emit()


class CompressionPreviewDialog(QDialog):
    """压缩预览：摘要（可编辑）+ 版本历史 + 接受/重试/拒绝/回退。

    所有操作经 :class:`CompressionService`；对话框不直接碰仓库。
    失败版本如实显示错误，不出现在可接受列表里。
    """

    # 操作结果外发，供上层刷新占用条与状态
    accepted = Signal(str)  # version_id
    retry_requested = Signal(str)  # version_id
    rolled_back = Signal(str)  # branch_id

    def __init__(
        self,
        service: CompressionService,
        version_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._version_id = version_id
        self.setWindowTitle("压缩预览")
        self.resize(720, 540)

        version = service.get(version_id)
        if version is None:
            raise ValueError(f"压缩版本不存在：{version_id}")
        self._branch_id = version.branch_id

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：版本历史
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("版本历史"))
        self._versions = QListWidget()
        self._versions.currentItemChanged.connect(self._on_version_select)
        left_layout.addWidget(self._versions, 1)
        splitter.addWidget(left)

        # 右：摘要（可编辑）+ 元信息 + 操作
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._meta = QLabel("")
        self._meta.setProperty("hint", True)
        self._meta.setWordWrap(True)
        right_layout.addWidget(self._meta)

        self._summary = QPlainTextEdit()
        self._summary.setPlaceholderText("摘要内容…")
        right_layout.addWidget(self._summary, 1)

        ops = QHBoxLayout()
        self._accept_btn = QPushButton("接受并启用")
        self._accept_btn.setProperty("accent", True)
        self._accept_btn.clicked.connect(self._on_accept)
        ops.addWidget(self._accept_btn)
        retry_btn = QPushButton("重试")
        retry_btn.clicked.connect(self._on_retry)
        ops.addWidget(retry_btn)
        reject_btn = QPushButton("拒绝")
        reject_btn.clicked.connect(self._on_reject)
        ops.addWidget(reject_btn)
        rollback_btn = QPushButton("回退到未压缩")
        rollback_btn.setProperty("flat", True)
        rollback_btn.clicked.connect(self._on_rollback)
        ops.addWidget(rollback_btn)
        ops.addStretch(1)
        right_layout.addLayout(ops)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 520])
        root.addWidget(splitter, 1)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        close_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(close_box)

        self._reload_versions()
        self._render(version)

    # ---------- 数据 ----------

    def _reload_versions(self) -> None:
        self._versions.clear()
        active = self._service.get_active(self._branch_id)
        for v in self._service.list_versions(self._branch_id):
            status_text = {
                "draft": "草稿",
                "previewed": "预览",
                "accepted": "已接受",
                "rejected": "已拒绝",
                "failed": "失败",
            }.get(v.status.value, v.status.value)
            mark = "● " if active is not None and v.id == active.id else ""
            error_mark = " ⚠" if v.status.value == "failed" else ""
            item = QListWidgetItem(
                f"{mark}{v.created_at.astimezone():%m-%d %H:%M}  {status_text}{error_mark}"
            )
            item.setData(Qt.ItemDataRole.UserRole, v.id)
            self._versions.addItem(item)

    def _render(self, version: CompressionVersion) -> None:
        self._version_id = version.id
        if version.status.value == "failed":
            self._meta.setText(f"生成失败：{version.error or '未知错误'}。原始上下文未改动。")
            self._summary.setPlainText("")
            self._summary.setEnabled(False)
            self._accept_btn.setEnabled(False)
            return
        self._summary.setEnabled(True)
        self._accept_btn.setEnabled(True)
        self._meta.setText(
            f"压缩前约 {version.tokens_before} tokens"
            f" → 压缩后约 {version.tokens_after} tokens（估算）"
            f"　·　压缩模型 {version.compression_model_id}"
            f"　·　提示词 v{version.prompt_version}"
            f"　·　白名单 {len(version.whitelist_message_ids)} 条原文保留"
        )
        # 编辑版优先，否则生成版
        self._summary.setPlainText(version.effective_summary)

    def _on_version_select(
        self, current: QListWidgetItem | None, _prev: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        version = self._service.get(current.data(Qt.ItemDataRole.UserRole))
        if version is not None:
            self._render(version)

    # ---------- 操作 ----------

    def _on_accept(self) -> None:
        # 用户可能改过摘要——先存编辑版再接受
        edited = self._summary.toPlainText()
        current = self._service.get(self._version_id)
        if current is not None and edited != current.effective_summary:
            self._service.edit_summary(self._version_id, edited)
        if self._service.accept(self._version_id):
            self._reload_versions()
            self.accepted.emit(self._version_id)

    def _on_retry(self) -> None:
        # 重试：通知上层重新生成（产出新版本），本对话框不关
        self.retry_requested.emit(self._version_id)

    def _on_reject(self) -> None:
        if self._service.reject(self._version_id):
            self._reload_versions()

    def _on_rollback(self) -> None:
        if self._service.rollback(self._branch_id):
            self._reload_versions()
            self.rolled_back.emit(self._branch_id)


class CompressionThinkingPanel(FloatingPanel):
    """压缩期间的思考过程悬浮窗（内建悬浮窗，见 ``ui/floating.py``）。

    压缩模型的输出不是对话的一轮，思考流不进消息区气泡，在这里单独展示。
    只读、只在内存里：思考不落库，压缩版本只存摘要。
    停在底部时随新内容自动跟随；往上翻阅读就不再强拉回底部，滚回底部恢复跟随。
    """

    THINKING = "压缩模型正在思考…"
    SUMMARIZING = "思考结束，正在生成摘要…"
    FINISHED = "压缩已结束"

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "压缩 · 思考过程", width=520)
        self._status = QLabel(self.THINKING)
        self._status.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
        )
        self.content_layout.addWidget(self._status)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setFixedHeight(260)  # 定高滚动：流式追加不把面板越撑越高
        self._text.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
        )
        self.content_layout.addWidget(self._text)

        # 贴底跟随：内容增高靠 rangeChanged 跟进（排版是延迟的，
        # 追加当下读到的 maximum() 还是旧值）。
        self._follow = True
        bar = self._text.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range_changed)

    @property
    def thinking_text(self) -> str:
        return self._text.toPlainText()

    @property
    def status_text(self) -> str:
        return self._status.text()

    def append(self, delta: str) -> None:
        """追加一段思考。用独立游标插在文末：不动用户正在看的选区与光标。"""
        cursor = QTextCursor(self._text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)

    def note_summarizing(self) -> None:
        """思考结束、模型开始写摘要正文（正文不在这里展示，留给压缩预览）。"""
        self._status.setText(self.SUMMARIZING)

    def note_finished(self) -> None:
        self._status.setText(self.FINISHED)

    def _on_scrolled(self, value: int) -> None:
        self._follow = value >= self._text.verticalScrollBar().maximum()

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self._text.verticalScrollBar().setValue(maximum)
