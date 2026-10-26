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
    QLayout,
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
        from limbowave.ui.background_tasks import BackgroundTasks

        self._jobs = BackgroundTasks(self)
        self._reload_generation = 0
        self.service = service
        self.conversation_id = conversation_id
        self.branch_id = branch_id
        self._selected_id: str | None = None
        self._global_states: dict[str, str] = {}
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
        if conversation_id is not None and branch_id is not None:
            from limbowave.ui.scratchpad_panel import ArchivedMemoryPanel, ScratchpadPanel

            notes = self._add_page(
                "透明便签", "AI 的短期上下文，不是正式记忆或隐藏推理。仅当前分支可用，"
                "每轮提供给模型；你可以查看、编辑和清空，切勿存放密钥。",
            )
            self.scratchpad = ScratchpadPanel(service, conversation_id, branch_id)
            notes.addWidget(self.scratchpad, 1)
            archive = self._add_page(
                "归档记忆", "已结束事项的记忆不再注入对话。可主动检索，或恢复到当前分支。",
            )
            self.archive = ArchivedMemoryPanel(service, conversation_id, branch_id)
            archive.addWidget(self.archive, 1)
            self.archive.changed.connect(self.reload)
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
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            scroll = QScrollArea()
            scroll.setObjectName(
                "memoryEditorScroll" if title == "记忆编辑" else "memoryPageScroll"
            )
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
        self.save_settings_button.clicked.connect(self._save_settings)
        layout.addWidget(self.save_settings_button)
        layout.addStretch(1)

    def _build_editor_page(self) -> None:
        layout = self._add_page(
            "记忆编辑",
            "双栏滚动浏览记忆，首行为标题；双击卡片或按 Enter 编辑正文。"
            "内置预设也可修改或删除，修改不会被自动覆盖，删除后不会自动补回。",
            scrollable=True,
        )
        # 先绑定父级再设置可见性，避免加入布局前短暂弹成独立窗口。
        self.scope = QComboBox(self)
        self.scope.setObjectName("memoryScope")
        self.scope.setAccessibleName("记忆范围")
        self.scope.addItem("会话记忆 · 当前分支", "session")
        self.scope.addItem("全局记忆 · 仅本会话定制", "global")
        self.scope.setVisible(self.conversation_id is not None)
        layout.addWidget(self.scope)
        self.scope_hint = QLabel(self)
        self.scope_hint.setObjectName("memoryPageHint")
        self.scope_hint.setWordWrap(True)
        self.scope_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_hint.setVisible(self.conversation_id is not None)
        layout.addWidget(self.scope_hint)
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
        self.promote_button = QPushButton("升格为全局", self)
        self.promote_button.setVisible(self.branch_id is not None)
        self.promote_button.clicked.connect(self._promote)
        row.addWidget(self.promote_button)
        self.restore_button = QPushButton("恢复全局版本")
        self.restore_button.clicked.connect(self._restore_global)
        row.addWidget(self.restore_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self._sync_scope_controls()

    @property
    def _customizing_global(self) -> bool:
        return self.conversation_id is not None and self.scope.currentData() == "global"

    def _sync_scope_controls(self) -> None:
        customizing = self._customizing_global
        self.new_button.setVisible(not customizing)
        self.promote_button.setVisible(self.branch_id is not None and not customizing)
        self.restore_button.setVisible(customizing)
        self.delete_button.setText("仅本会话删除" if customizing else "删除")
        self.scope_hint.setText(
            "仅本会话生效，所有分支共用；不影响全局原文或其他会话。"
            "未定制的条目继续跟随全局更新，已删除的条目不用于对话，可恢复全局版本。"
            if customizing else "当前分支的独立记忆；切换上方范围可为本会话定制全局记忆。"
        )
        self.items.empty_state.setText(
            "暂无全局记忆\n可在“设置 → 记忆”中添加，再为本会话单独定制。"
            if customizing else "暂无记忆\n点击“添加记忆”创建第一条记忆。"
        )

    def _scope_changed(self, _index: int) -> None:
        self._close_editor()
        self.status.clear()
        self._sync_scope_controls()
        self.reload()

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
        self.setEnabled(False)
        self._reload_generation += 1
        generation = self._reload_generation
        conversation_id, branch_id = self.conversation_id, self.branch_id
        service = self.service
        customizing = self._customizing_global

        def read() -> tuple[list[MemoryItem], MemorySettings, str, dict[str, str]]:
            settings = service.settings()
            policy = (
                service.policy(conversation_id).value
                if conversation_id else settings.default_policy
            )
            if customizing and conversation_id is not None:
                entries = service.list_session_global(conversation_id)
                return (
                    [entry.memory for entry in entries], settings, policy,
                    {entry.memory.id: entry.state for entry in entries},
                )
            return service.list(conversation_id, branch_id), settings, policy, {}

        def apply(data: tuple[list[MemoryItem], MemorySettings, str, dict[str, str]]) -> None:
            if generation != self._reload_generation:
                return
            self.setEnabled(True)
            memories, settings, policy, self._global_states = data
            self.items.clear()
            for memory in memories:
                title = memory.content.splitlines()[0]
                state = self._global_states.get(memory.id)
                label = {
                    "inherited": "继承全局", "modified": "本会话已修改", "deleted": "本会话已删除",
                }.get(state or "", "")
                item = QListWidgetItem(f"[{label}] {title}" if label else title)
                item.setData(Qt.ItemDataRole.UserRole, memory)
                item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, label)
                item.setToolTip(f"{label}\n{memory.content}" if label else memory.content)
                self.items.addItem(item)
            self._select(None, None)
            self.global_interval.setValue(settings.global_interval)
            self.session_interval.setValue(settings.session_interval)
            self.policy.setCurrentIndex(self.policy.findData(policy))
            default = settings.default_policy
            self.policy_hint.setText(
                f"全局默认：{'每次询问' if default == 'ask' else '自动允许'}。"
                "记忆在首轮及到达各自间隔时提醒；内容修改后下一轮刷新。"
            )

        def failed(exc: Exception) -> None:
            if generation != self._reload_generation:
                return
            self.setEnabled(True)
            self.items.clear()
            self._global_states.clear()
            self._select(None, None)
            self.status.setText(str(exc))

        self._jobs.submit(read, apply, failed)

    def _new(self) -> None:
        if self._customizing_global:
            return
        self.items.setCurrentRow(-1)
        self._open_editor()

    def _select(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        self._selected_id = item.data(Qt.ItemDataRole.UserRole).id if item is not None else None
        state = self._global_states.get(self._selected_id or "")
        self.delete_button.setEnabled(item is not None and state != "deleted")
        self.promote_button.setEnabled(item is not None and not self._customizing_global)
        self.restore_button.setEnabled(state in ("modified", "deleted"))

    def _edit(self, item: QListWidgetItem) -> None:
        memory = item.data(Qt.ItemDataRole.UserRole)
        if self._global_states.get(memory.id) == "deleted":
            self.status.setText("此条目已在本会话删除；请先恢复全局版本，再进行编辑。")
            return
        self._open_editor(memory)

    def _floating_parent(self) -> QWidget:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, FloatingPanel):
                return parent
            parent = parent.parentWidget()
        return self.window()

    def _open_editor(self, memory: MemoryItem | None = None) -> None:
        self._close_editor()
        customizing = self._customizing_global
        content = QWidget()
        layout = QVBoxLayout(content)
        if customizing:
            hint = QLabel("修改仅对本会话生效，不会更改全局原文或其他会话。")
            hint.setWordWrap(True)
            layout.addWidget(hint)
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
            parent,
            "定制全局记忆 · 仅本会话" if customizing else "编辑记忆" if memory else "添加记忆",
            content,
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
            text = editor.toPlainText()
            service = self.service
            save.setEnabled(False)

            def ready(_value: object) -> None:
                if self._editor_panel is panel:
                    panel.close_panel()
                self.reload()
                self.status.setText(
                    "已保存，仅本会话生效；下一轮对话使用新内容。" if customizing else "已保存"
                )

            def failed(exc: Exception) -> None:
                if self._editor_panel is panel:
                    save.setEnabled(True)
                    error.setText(str(exc))
                    editor.setFocus()

            def write() -> None:
                if customizing:
                    if conversation_id is None or item_id is None:
                        raise ValueError("请选择需要定制的全局记忆")
                    service.save_session_global(item_id, text, conversation_id)
                else:
                    service.save(text, conversation_id, branch_id, item_id=item_id)

            self._jobs.submit(write, ready, failed)

        save.clicked.connect(submit)
        editor.setFocus()

    def _editor_closed(self) -> None:
        self._editor_panel = None

    def _close_editor(self) -> None:
        if self._editor_panel is not None:
            self._editor_panel.close_panel()

    def _perform(self, action: Callable[[], object], message: str = "已保存") -> None:
        self.setEnabled(False)

        def ready(_value: object) -> None:
            self.setEnabled(True)
            self.reload()
            self.status.setText(message)

        def failed(exc: Exception) -> None:
            self.setEnabled(True)
            self.status.setText(str(exc))

        self._jobs.submit(action, ready, failed)

    def _save_settings(self) -> None:
        service, conversation_id = self.service, self.conversation_id
        if conversation_id:
            policy = MemoryPolicy(self.policy.currentData())
            self._perform(lambda: service.set_policy(conversation_id, policy))
        else:
            settings = MemorySettings(
                global_interval=self.global_interval.value(),
                session_interval=self.session_interval.value(),
                default_policy=self.policy.currentData(),
            )
            self._perform(lambda: service.save_settings(settings))

    def _delete(self) -> None:
        conversation_id, branch_id = self.conversation_id, self.branch_id
        item_id = self._selected_id
        if item_id is None:
            return
        if self._customizing_global and conversation_id is not None:
            service = self.service
            ask_confirm(
                self._floating_parent(),
                "仅在本会话删除这条全局记忆？",
                "此会话的所有分支将不再使用这条记忆。全局原文与其他会话不受影响，"
                "之后可通过“恢复全局版本”重新启用。",
                lambda yes: self._perform(
                    lambda: service.delete_session_global(item_id, conversation_id),
                    "已在本会话删除；下一轮对话生效，可随时恢复。",
                ) if yes else None,
            )
            return
        ask_confirm(
            self._floating_parent(),
            "删除记忆？",
            "将从当前有效记忆中移除；已有历史分支快照保持不变。",
            lambda yes: (
                self._perform(
                    lambda: self.service.delete(item_id, conversation_id, branch_id)
                )
                if yes
                else None
            ),
        )

    def _restore_global(self) -> None:
        item_id, conversation_id = self._selected_id, self.conversation_id
        if not self._customizing_global or item_id is None or conversation_id is None:
            return
        service = self.service
        self._perform(
            lambda: service.restore_session_global(item_id, conversation_id),
            "已恢复当前全局版本；下一轮对话生效。",
        )

    def _promote(self) -> None:
        if self._customizing_global:
            return
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
