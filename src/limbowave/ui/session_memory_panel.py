"""全局及会话记忆管理，持久化只经过 MemoryService。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QHideEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.memory_service import MemoryService
from limbowave.domain.memory import MemoryItem, MemoryPolicy, MemorySettings
from limbowave.ui.animated_stack import AnimatedPageStack, AnimatedTabBar
from limbowave.ui.floating import FloatingPanel, ask_confirm, open_panel
from limbowave.ui.memory_card_list import MemoryCardList


class MemoryPanel(QWidget):
    def __init__(
        self,
        service: MemoryService,
        parent: QWidget | None = None,
        *,
        conversation_id: str | None = None,
        branch_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.conversation_id = conversation_id
        self.branch_id = branch_id
        self._selected_id: str | None = None
        self._editor_panel: FloatingPanel | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        tab_host = QWidget()
        tab_host.setObjectName("memorySubtabHost")
        tab_layout = QHBoxLayout(tab_host)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)
        tab_layout.addStretch(1)
        self._subtabs = AnimatedTabBar(indicator_name="memorySubtabIndicator")
        self._subtabs.setObjectName("memorySubtabs")
        self._subtabs.setShape(AnimatedTabBar.Shape.RoundedNorth)
        self._subtabs.setExpanding(False)
        self._subtabs.setElideMode(Qt.TextElideMode.ElideNone)
        self._subtabs.setDocumentMode(True)
        self._subtabs.setDrawBase(False)
        tab_layout.addWidget(self._subtabs, 5)
        tab_layout.addStretch(1)
        root.addWidget(tab_host)

        self._stack = AnimatedPageStack(orientation=Qt.Orientation.Horizontal)
        self._stack.setObjectName("memorySubtabStack")
        self._stack.setMinimumHeight(300)
        root.addWidget(self._stack, 1)
        self._build_injection_page()
        self._build_editor_page()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        self._subtabs.currentChanged.connect(self._stack.set_index)
        self._subtabs.refresh_width_constraints(max(0, self.width() - 8))
        self._subtabs.sync_indicator()
        self.reload()

    def _add_page(self, title: str, hint: str, *, scrollable: bool = False) -> QVBoxLayout:
        page = QWidget()
        page.setObjectName("memoryPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("memoryPageTitle")
        layout.addWidget(heading)
        description = QLabel(hint)
        description.setObjectName("memoryPageHint")
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(description)
        self._subtabs.addTab(title)
        if scrollable:
            scroll = QScrollArea()
            scroll.setObjectName("memoryPageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self._stack.add_page(scroll)
        else:
            self._stack.add_page(page)
        return layout

    def _build_injection_page(self) -> None:
        layout = self._add_page(
            "记忆注入",
            f"当前分支：{self.branch_id}" if self.branch_id else "全局记忆 · 对所有会话生效",
            scrollable=True,
        )
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(11)
        self.policy = QComboBox()
        if self.conversation_id:
            self.policy.addItem("继承全局默认", MemoryPolicy.INHERIT.value)
        self.policy.addItem("每次询问", MemoryPolicy.ASK.value)
        self.policy.addItem("自动允许添加", MemoryPolicy.ALLOW.value)
        form.addRow("模型写入会话记忆", self.policy)
        self.global_interval = QSpinBox(self)
        self.session_interval = QSpinBox(self)
        for spin in (self.global_interval, self.session_interval):
            spin.setRange(1, 30)
            spin.setSuffix(" 轮")
            spin.setVisible(not self.conversation_id)
        if not self.conversation_id:
            form.addRow("全局记忆提醒间隔", self.global_interval)
            form.addRow("会话记忆提醒间隔", self.session_interval)
        layout.addLayout(form)
        self.policy_hint = QLabel()
        self.policy_hint.setWordWrap(True)
        layout.addWidget(self.policy_hint)
        self.save_settings_button = QPushButton("保存记忆设置")
        self.save_settings_button.clicked.connect(lambda: self._perform(self._save_settings))
        layout.addWidget(self.save_settings_button)
        layout.addStretch(1)

    def _build_editor_page(self) -> None:
        layout = self._add_page(
            "记忆编辑",
            "双栏滚动浏览记忆，首行为标题；双击卡片或按 Enter 编辑正文。",
        )
        self.items = MemoryCardList()
        self.items.setMinimumHeight(120)
        self.items.currentItemChanged.connect(self._select)
        self.items.itemActivated.connect(self._edit)
        layout.addWidget(self.items, 1)
        row = QHBoxLayout()
        self.new_button = QPushButton("添加记忆")
        self.new_button.clicked.connect(self._new)
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self._delete)
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        self.promote_button = QPushButton("升格为全局")
        self.promote_button.setVisible(self.branch_id is not None)
        self.promote_button.clicked.connect(self._promote)
        row.addWidget(self.promote_button)
        row.addStretch(1)
        layout.addLayout(row)

    @property
    def is_editing(self) -> bool:
        return self._editor_panel is not None

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._subtabs.refresh_width_constraints(max(0, self.width() - 8))

    def hideEvent(self, event: QHideEvent) -> None:
        self._close_editor()
        super().hideEvent(event)

    def reload(self) -> None:
        self.items.clear()
        for memory in self.service.list(self.conversation_id, self.branch_id):
            item = QListWidgetItem(memory.content.splitlines()[0])
            item.setData(Qt.ItemDataRole.UserRole, memory)
            item.setToolTip(memory.content)
            self.items.addItem(item)
        self._select(None, None)
        settings = self.service.settings()
        self.global_interval.setValue(settings.global_interval)
        self.session_interval.setValue(settings.session_interval)
        policy = (
            self.service.policy(self.conversation_id).value
            if self.conversation_id
            else settings.default_policy
        )
        self.policy.setCurrentIndex(self.policy.findData(policy))
        default = settings.default_policy
        self.policy_hint.setText(
            f"全局默认：{'每次询问' if default == 'ask' else '自动允许'}。"
            "记忆在首轮及到达各自间隔时提醒；内容修改后下一轮刷新。"
        )

    def _new(self) -> None:
        self.items.setCurrentRow(-1)
        self._open_editor()

    def _select(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        self._selected_id = item.data(Qt.ItemDataRole.UserRole).id if item is not None else None
        self.delete_button.setEnabled(item is not None)
        self.promote_button.setEnabled(item is not None)

    def _edit(self, item: QListWidgetItem) -> None:
        self._open_editor(item.data(Qt.ItemDataRole.UserRole))

    def _floating_parent(self) -> QWidget:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, FloatingPanel):
                return parent
            parent = parent.parentWidget()
        return self.window()

    def _open_editor(self, memory: MemoryItem | None = None) -> None:
        self._close_editor()
        content = QWidget()
        layout = QVBoxLayout(content)
        editor = QPlainTextEdit()
        editor.setPlaceholderText("输入需要长期保留的信息（1–4000字）")
        editor.setMinimumHeight(160)
        if memory is not None:
            editor.setPlainText(memory.content)
        layout.addWidget(editor)
        error = QLabel()
        error.setObjectName("memoryEditorError")
        error.setWordWrap(True)
        error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("memoryEditorCancel")
        save = QPushButton("保存")
        save.setObjectName("memoryEditorSave")
        save.setProperty("accent", True)
        row.addWidget(cancel)
        row.addWidget(save)
        layout.addLayout(row)
        parent = self._floating_parent()
        panel = open_panel(
            parent, "编辑记忆" if memory else "添加记忆", content,
            width=min(560, max(280, parent.width() - 28)),
        )
        self._editor_panel = panel
        panel.closed.connect(self._editor_closed)
        # 会话记忆本身位于悬浮窗内；子窗随父窗关闭，且点击子窗不会被当成外部点击。
        if isinstance(parent, FloatingPanel):
            parent.closed.connect(panel.close_panel)
        cancel.clicked.connect(panel.close_panel)
        item_id = memory.id if memory else None
        conversation_id, branch_id = self.conversation_id, self.branch_id

        def submit() -> None:
            if self._editor_panel is not panel:
                return
            try:
                self.service.save(
                    editor.toPlainText(), conversation_id, branch_id, item_id=item_id,
                )
            except Exception as exc:
                error.setText(str(exc))
                editor.setFocus()
                return
            panel.close_panel()
            self.reload()
            self.status.setText("已保存")

        save.clicked.connect(submit)
        editor.setFocus()

    def _editor_closed(self) -> None:
        self._editor_panel = None

    def _close_editor(self) -> None:
        if self._editor_panel is not None:
            self._editor_panel.close_panel()

    def _perform(self, action: Callable[[], object]) -> None:
        try:
            action()
            self.reload()
        except Exception as exc:
            self.status.setText(str(exc))
        else:
            self.status.setText("已保存")

    def _save_settings(self) -> None:
        if self.conversation_id:
            self.service.set_policy(self.conversation_id, MemoryPolicy(self.policy.currentData()))
        else:
            self.service.save_settings(
                MemorySettings(
                    global_interval=self.global_interval.value(),
                    session_interval=self.session_interval.value(),
                    default_policy=self.policy.currentData(),
                )
            )

    def _delete(self) -> None:
        item_id = self._selected_id
        if item_id is None:
            return
        ask_confirm(
            self._floating_parent(),
            "删除记忆？",
            "将从当前有效记忆中移除；已有历史分支快照保持不变。",
            lambda yes: (
                self._perform(
                    lambda: self.service.delete(item_id, self.conversation_id, self.branch_id)
                )
                if yes
                else None
            ),
        )

    def _promote(self) -> None:
        item_id, conversation_id, branch_id = (
            self._selected_id,
            self.conversation_id,
            self.branch_id,
        )
        if item_id is None or conversation_id is None or branch_id is None:
            return
        ask_confirm(
            self._floating_parent(),
            "升格为全局记忆？",
            "这条记忆将影响所有会话。原分支保留独立副本，后续修改不会相互同步。",
            lambda yes: (
                self._perform(lambda: self.service.promote(item_id, conversation_id, branch_id))
                if yes
                else None
            ),
        )
