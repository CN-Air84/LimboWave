"""站点删除确认：展示解绑影响，再通过拖动手柄显式提交。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QWidget

from limbowave.domain.models import LogicalModel
from limbowave.domain.providers import EndpointConfig
from limbowave.ui import theme
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.slide_confirm import SlideConfirm


class EndpointDeletePanel(FloatingPanel):
    def __init__(
        self,
        parent: QWidget,
        endpoint: EndpointConfig,
        bound_models: Sequence[LogicalModel],
        on_confirm: Callable[[], None],
    ) -> None:
        action = "解绑并删除站点" if bound_models else "删除站点"
        super().__init__(parent, action, width=440)
        self._on_confirm = on_confirm
        self._answered = False

        detail = QLabel(f"将删除站点「{endpoint.name}」（{endpoint.id}）。")
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setWordWrap(True)
        self.content_layout.addWidget(detail)

        if bound_models:
            summary = QLabel(f"同时解除以下 {len(bound_models)} 个逻辑模型与该站点的绑定：")
            summary.setWordWrap(True)
            self.content_layout.addWidget(summary)
            models = QListWidget()
            models.setAccessibleName("将解除绑定的逻辑模型")
            models.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            models.setTextElideMode(Qt.TextElideMode.ElideRight)
            models.setStyleSheet(
                f"QListWidget {{ background: {theme.BG_ELEVATED};"
                f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
                "QListWidget::item { padding: 4px 8px; }"
            )
            for model in bound_models:
                label = model.name if model.name == model.id else f"{model.name} · {model.id}"
                item = QListWidgetItem(label)
                item.setToolTip(label)
                models.addItem(item)
            row_height = max(26, models.sizeHintForRow(0))
            models.setFixedHeight(min(140, row_height * len(bound_models) + 8))
            self.content_layout.addWidget(models)

        note = QLabel("逻辑模型会保留，其他站点的绑定不受影响。该站点的实际模型记录将一并删除。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self.content_layout.addWidget(note)

        unavailable = sum(len(model.bindings) == 1 for model in bound_models)
        if unavailable:
            warning = QLabel(f"其中 {unavailable} 个模型将暂无可用站点，重新绑定后才能使用。")
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {theme.WARNING};")
            self.content_layout.addWidget(warning)

        self.slider = SlideConfirm(self)
        self.slider.setFixedHeight(40)
        self.slider.set_hint(
            "向右滑动，一键解绑并删除" if bound_models else "向右滑动，确认删除站点"
        )
        self.slider.confirmed.connect(self._confirm)
        self.content_layout.addWidget(self.slider)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close_panel)
        self.content_layout.addWidget(cancel, 0, Qt.AlignmentFlag.AlignRight)
        self.closed.connect(self._cancel_gesture)
        self.popup()
        cancel.setFocus()

    def _cancel_gesture(self) -> None:
        self.slider.setEnabled(False)
        self.slider.reset()

    def _confirm(self) -> None:
        if self._answered or self._closing:
            return
        self._answered = True
        # 先关闭确认框，避免失败提示被旧面板遮住；回调只允许执行一次。
        self.close_panel()
        self._on_confirm()
