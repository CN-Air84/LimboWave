"""权限面板（Phase 9 / 设计计划 §11.1）。

「用户可随时查看、撤销或缩小授权。」本对话框提供：

- **授权列表**：能力、资源边界（目录/域名/工作目录）、建立时间、备注；
- **撤销**：删掉一条授权（下次同类请求会重新询问）；
- **缩小**：把边界改窄（编辑允许目录/域名列表）——实现方式是撤销旧的、
  建立边界更小的新授权，而不是原地改（授权记录是审计的一部分，不原地篡改）；
- **审计**：列出最近的权限决策，含是否用户确认——「为什么放行/拒绝」可追溯。

只读 + 撤销/缩小；对话框自己不碰仓库，全部经 :class:`PermissionService`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.permission_service import PermissionService
from limbowave.domain.permissions import Capability, PermissionGrant
from limbowave.ui import theme
from limbowave.ui.floating import FloatingPanel

CAPABILITY_TEXT = {
    Capability.FILE_READ: "文件读取",
    Capability.FILE_WRITE: "文件写入",
    Capability.TERMINAL: "终端执行",
    Capability.MEMORY_WRITE: "会话记忆",
    Capability.CLOCK_READ: "当前日期和时间",
    Capability.NETWORK: "联网访问",
}

_DECISION_TEXT = {"allow": "放行", "confirm": "需确认", "deny": "拒绝"}


class PermissionsDialog(QDialog):
    """当前会话的授权与审计。"""

    def __init__(
        self,
        service: PermissionService,
        conversation_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        from limbowave.ui.background_tasks import BackgroundTasks

        self._jobs = BackgroundTasks(self)
        self._grant_cache: list[PermissionGrant] = []
        self._reload_generation = 0
        self._service = service
        self._conversation_id = conversation_id
        self.setWindowTitle("会话权限")
        self.resize(760, 520)

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：授权列表 + 操作
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("已授予的权限"))
        self._grants = QListWidget()
        self._grants.currentItemChanged.connect(self._on_select)
        left_layout.addWidget(self._grants, 1)
        ops = QHBoxLayout()
        revoke_btn = QPushButton("撤销")
        revoke_btn.clicked.connect(self._on_revoke)
        ops.addWidget(revoke_btn)
        narrow_btn = QPushButton("缩小范围")
        narrow_btn.setProperty("flat", True)
        narrow_btn.clicked.connect(self._on_narrow)
        ops.addWidget(narrow_btn)
        left_layout.addLayout(ops)
        splitter.addWidget(left)

        # 右：边界详情 + 审计
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._detail = QLabel("选择左侧一条授权查看边界")
        self._detail.setWordWrap(True)
        self._detail.setProperty("hint", True)
        right_layout.addWidget(self._detail)
        right_layout.addWidget(QLabel("最近的权限决策"))
        self._audit = QListWidget()
        right_layout.addWidget(self._audit, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 460])
        root.addWidget(splitter, 1)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        close_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(close_box)

        self.reload()

    # ---------- 数据 ----------

    def reload(self) -> None:
        self._reload_generation += 1
        generation = self._reload_generation
        service, conversation_id = self._service, self._conversation_id

        def read() -> tuple[list[PermissionGrant], list[Any]]:
            return service.list_grants(conversation_id), service.list_audit(conversation_id)[-50:]

        def apply(data: tuple[list[PermissionGrant], list[Any]]) -> None:
            if generation != self._reload_generation:
                return
            grants, records = data
            self._grant_cache = grants
            self._grants.clear()
            for grant in grants:
                item = QListWidgetItem(self._describe(grant))
                item.setData(Qt.ItemDataRole.UserRole, grant.id)
                self._grants.addItem(item)

            self._audit.clear()
            for record in records:
                mark = "· 已确认" if record.user_confirmed else ""
                when = record.created_at.astimezone().strftime("%m-%d %H:%M:%S")
                self._audit.addItem(
                    QListWidgetItem(
                        f"{when}  {record.tool_name}  "
                        f"{_DECISION_TEXT.get(record.decision.value, record.decision.value)}{mark}"
                    )
                )

        self._jobs.submit(read, apply, lambda exc: self._detail.setText(str(exc)))

    def _change(self, work: Callable[[], object]) -> None:
        self.setEnabled(False)

        def ready(_value: object) -> None:
            self.setEnabled(True)
            self.reload()

        def failed(exc: Exception) -> None:
            self.setEnabled(True)
            self._detail.setText(str(exc))

        self._jobs.submit(work, ready, failed)

    def _selected_id(self) -> str | None:
        item = self._grants.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    @staticmethod
    def _describe(grant: PermissionGrant) -> str:
        capability = CAPABILITY_TEXT.get(grant.capability, grant.capability.value)
        return f"{capability} · {_boundary_text(grant)}"

    def _on_select(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        grant_id = current.data(Qt.ItemDataRole.UserRole)
        grant = next(
            (g for g in self._grant_cache if g.id == grant_id),
            None,
        )
        if grant is None:
            self._detail.setText("（授权已不存在）")
            return
        lines = [
            f"能力：{CAPABILITY_TEXT.get(grant.capability, grant.capability.value)}",
            f"建立时间：{grant.created_at.astimezone():%Y-%m-%d %H:%M:%S}",
            f"边界：{_boundary_text(grant)}",
        ]
        if grant.note:
            lines.append(f"备注：{grant.note}")
        self._detail.setText("\n".join(lines))

    # ---------- 操作 ----------

    def _on_revoke(self) -> None:
        grant_id = self._selected_id()
        if grant_id is None:
            return
        self._change(lambda: self._service.revoke(grant_id))

    def _on_narrow(self) -> None:
        """缩小范围：撤销原授权，建立边界更小的新授权（不改写历史记录）。"""
        grant_id = self._selected_id()
        if grant_id is None:
            return
        grant = next(
            (g for g in self._grant_cache if g.id == grant_id),
            None,
        )
        if grant is None:
            return
        from limbowave.ui.floating import ask_prompt

        if grant.capability in (Capability.FILE_READ, Capability.FILE_WRITE):
            current = ", ".join(grant.allowed_paths)
            label = "允许的目录（逗号分隔）："
        else:
            current = ", ".join(grant.allowed_domains)
            label = "允许的域名（逗号分隔）："

        def _narrow(text: str) -> None:
            values = tuple(v.strip() for v in text.split(",") if v.strip())
            if not values:
                return
            def work() -> None:
                # 撤销旧的 + 建立范围更小的新的
                self._service.revoke(grant_id)
                if grant.capability in (Capability.FILE_READ, Capability.FILE_WRITE):
                    self._service.grant(
                        self._conversation_id,
                        grant.capability,
                        allowed_paths=values,
                        note=f"由 {grant.id} 缩小范围",
                    )
                else:
                    self._service.grant(
                        self._conversation_id,
                        grant.capability,
                        allowed_domains=values,
                        note=f"由 {grant.id} 缩小范围",
                    )
            self._change(work)

        ask_prompt(self, "缩小授权范围", label, _narrow, default_text=current)
        self._detail.setText("授权范围已缩小。")


def _boundary_text(grant: PermissionGrant) -> str:
    parts: list[str] = []
    if grant.allowed_paths:
        parts.append("目录 " + ", ".join(grant.allowed_paths))
    if grant.allowed_domains:
        parts.append("域名 " + ", ".join(grant.allowed_domains))
    if grant.allowed_working_directories:
        parts.append("工作目录 " + ", ".join(grant.allowed_working_directories))
    if grant.command_classes:
        parts.append("命令类别 " + ", ".join(grant.command_classes))
    return "；".join(parts) or "（无边界限制）"


def open_custom_permissions(
    parent: QWidget,
    selected: set[Capability],
    on_apply: Callable[[set[Capability]], None],
    *,
    anchor: QWidget | None = None,
) -> FloatingPanel:
    """在权限按钮旁打开细粒度能力配置悬浮窗。"""
    panel = FloatingPanel(parent, "自定义权限", width=420)
    caption = QLabel("选择模型在本会话中可使用的能力。未选中的调用会直接拒绝。")
    caption.setWordWrap(True)
    caption.setProperty("hint", True)
    panel.content_layout.addWidget(caption)

    rows = (
        (Capability.FILE_READ, "文件读取", "读取当前工作区内的文件与目录"),
        (Capability.FILE_WRITE, "文件写入", "创建或修改当前工作区内的文件"),
        (Capability.TERMINAL, "终端执行", "运行 PowerShell 命令"),
        (Capability.NETWORK, "联网访问", "搜索网页或读取网址"),
    )
    boxes: dict[Capability, QCheckBox] = {}
    for capability, title, detail in rows:
        box = QCheckBox(f"{title}    {detail}")
        box.setChecked(capability in selected)
        panel.content_layout.addWidget(box)
        boxes[capability] = box

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    cancel = QPushButton("取消")
    cancel.clicked.connect(panel.close_panel)
    buttons.addWidget(cancel)
    apply_button = QPushButton("应用")
    apply_button.setProperty("accent", True)
    buttons.addWidget(apply_button)
    panel.content_layout.addLayout(buttons)

    def _apply() -> None:
        on_apply({capability for capability, box in boxes.items() if box.isChecked()})
        panel.close_panel()

    apply_button.clicked.connect(_apply)
    panel.popup(anchor)
    return panel


__all__ = [
    "CAPABILITY_TEXT",
    "PermissionsDialog",
    "open_custom_permissions",
    "theme",
]
