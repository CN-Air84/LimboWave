"""导出悬浮窗（§十四.2 的界面升级）。

从「导出 HTML 一个按钮 + 原生文件对话框」改成**应用内悬浮窗**：
用户在面板里选范围（会话/分支，支持全选与时间筛选）、格式（md/txt/json/html）、
文件名与位置，点「导出」一次性写出。文件位置仍用系统目录选择器——
那是一个目录路径的获取手段，不是应用弹窗；其余交互全部在面板内完成。

范围的组织方式：列表以「会话」为单位，每个会话下勾选要导出的**分支**。
时间筛选作用于会话的最新活动时间（最近 N 天 / 全部）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.floating import FloatingPanel

Formats = ("md", "txt", "json", "html")
_TIME_CHOICES = (("全部", 0), ("最近 1 天", 1), ("最近 7 天", 7), ("最近 30 天", 30))


class _ConversationRow(QWidget):
    """一个会话一行：标题 + 分支勾选。折叠成一条 checkbox，勾上即导出其默认分支。"""

    def __init__(self, title: str, branches: list[tuple[str, str]], last_active: datetime) -> None:
        super().__init__()
        self.conversation_title = title
        self.branches = branches  # (branch_id, label)
        self.last_active = last_active
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(2)
        top = QHBoxLayout()
        self.checkbox = QCheckBox(title)
        self.checkbox.setChecked(True)
        top.addWidget(self.checkbox)
        top.addStretch(1)
        self._stamp = QLabel(last_active.astimezone().strftime("%m-%d %H:%M"))
        self._stamp.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px;")
        top.addWidget(self._stamp)
        layout.addLayout(top)
        # 多分支会话展开为分支级勾选；单分支不展开（绝大多数会话只有主线）
        if len(branches) > 1:
            self._branch_checks: list[tuple[str, QCheckBox]] = []
            for branch_id, label in branches:
                check = QCheckBox(f"　└ {label}")
                check.setChecked(branch_id == branches[-1][0])  # 默认只勾主线
                self._branch_checks.append((branch_id, check))
                layout.addWidget(check)
        else:
            self._branch_checks = []

    def selected_branches(self) -> list[str]:
        if not self.checkbox.isChecked():
            return []
        if self._branch_checks:
            return [bid for bid, check in self._branch_checks if check.isChecked()]
        return [self.branches[-1][0]]  # 单分支：主线

    def selected_labels(self) -> list[str]:
        if not self.checkbox.isChecked():
            return []
        if self._branch_checks:
            labels: list[str] = []
            for index, (_bid, check) in enumerate(self._branch_checks):
                if check.isChecked() and index < len(self.branches):
                    labels.append(self.branches[index][1])
            return labels
        return [self.branches[-1][1]]


class ExportPanel(FloatingPanel):
    """导出悬浮窗：范围（多选+时间筛选）→ 格式 → 文件名 → 位置 → 导出。"""

    def __init__(
        self,
        parent: QWidget,
        rows: list[tuple[str, str, list[tuple[str, str]], datetime]],
        *,
        default_dir: str = "",
        on_export: Callable[[list[str], list[str], str, Path], None],
    ) -> None:
        """``rows``：(conversation_id, title, branches, last_active)。

        ``on_export(branch_ids, branch_labels, fmt, directory)`` 由上层执行写出，
        结果反馈也由上层负责（面板在点击后立即关闭）。
        """
        super().__init__(parent, "导出", width=470)
        self._on_export = on_export
        root = self.content_layout

        # ---- 范围 ----
        scope_label = QLabel("范围（按会话勾选，时间筛选作用于最近活动）")
        scope_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;")
        root.addWidget(scope_label)

        filter_row = QHBoxLayout()
        self._time_filter = QComboBox()
        for label, _days in _TIME_CHOICES:
            self._time_filter.addItem(label)
        self._time_filter.currentIndexChanged.connect(self._apply_time_filter)
        filter_row.addWidget(self._time_filter)
        select_all = QPushButton("全选")
        select_all.setProperty("flat", True)
        select_all.clicked.connect(lambda: self._set_all(True))
        filter_row.addWidget(select_all)
        select_none = QPushButton("全不选")
        select_none.setProperty("flat", True)
        select_none.clicked.connect(lambda: self._set_all(False))
        filter_row.addWidget(select_none)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        self._rows_host = QWidget()
        rows_layout = QVBoxLayout(self._rows_host)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(2)
        self._rows: list[_ConversationRow] = []
        for _cid, title, branches, last_active in rows:
            row = _ConversationRow(title, branches, last_active)
            self._rows.append(row)
            rows_layout.addWidget(row)
        rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_host)
        scroll.setFixedHeight(180)
        scroll.setStyleSheet("QScrollArea { border: 1px solid " + theme.BORDER + "; }")
        root.addWidget(scroll, 1)

        # ---- 格式 ----
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("格式"))
        self._fmt: dict[str, QRadioButton] = {}
        for fmt in Formats:
            button = QRadioButton(fmt.upper())
            button.setChecked(fmt == "md")
            self._fmt[fmt] = button
            fmt_row.addWidget(button)
        fmt_row.addStretch(1)
        root.addLayout(fmt_row)

        # ---- 文件名 ----
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("文件名"))
        self._name = QLineEdit(self._default_name())
        name_row.addWidget(self._name, 1)
        root.addLayout(name_row)

        # ---- 位置 ----
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("位置"))
        self._dir = QLineEdit(default_dir)
        dir_row.addWidget(self._dir, 1)
        browse = QPushButton("浏览…")
        browse.setProperty("flat", True)
        browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse)
        root.addLayout(dir_row)

        # ---- 动作 ----
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close_panel)
        actions.addWidget(cancel)
        self._go = QPushButton("导出")
        self._go.setProperty("accent", True)
        self._go.clicked.connect(self._do_export)
        actions.addWidget(self._go)
        root.addLayout(actions)

        self.popup()

    # ---------- 交互 ----------

    def _default_name(self) -> str:
        return f"limbowave-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def _browse_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择导出目录", self._dir.text() or "")
        if chosen:
            self._dir.setText(chosen)

    def _selected_rows(self) -> list[_ConversationRow]:
        cutoff_days = dict(_TIME_CHOICES).get(self._time_filter.currentText(), 0)
        if not cutoff_days:
            return self._rows
        cutoff = datetime.now(UTC).astimezone() - timedelta(days=cutoff_days)
        return [row for row in self._rows if row.last_active >= cutoff]

    def _apply_time_filter(self) -> None:
        visible = {id(row) for row in self._selected_rows()}
        for row in self._rows:
            row.setVisible(id(row) in visible)

    def _set_all(self, checked: bool) -> None:
        for row in self._selected_rows():
            row.checkbox.setChecked(checked)
            for _bid, check in row._branch_checks:
                check.setChecked(checked)

    def _do_export(self) -> None:
        branch_ids: list[str] = []
        labels: list[str] = []
        for row in self._selected_rows():
            ids = row.selected_branches()
            branch_ids.extend(ids)
            labels.extend(row.selected_labels())
        if not branch_ids:
            return
        fmt = next((key for key, box in self._fmt.items() if box.isChecked()), "md")
        directory = self._dir.text().strip() or str(Path.home())
        name = self._name.text().strip() or self._default_name()
        self.close_panel()
        self._on_export(branch_ids, labels, fmt, Path(directory) / name)
