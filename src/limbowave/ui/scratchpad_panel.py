"""Transparent short-lived context, kept separate from formal memory."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.memory_service import MAX_MEMORY_CHARS, MemoryService
from limbowave.domain.memory import MemoryItem, Scratchpad
from limbowave.ui.background_tasks import BackgroundTasks
from limbowave.ui.floating import FloatingPanel, ask_confirm


def _floating_parent(widget: QWidget) -> QWidget:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, FloatingPanel):
            return parent
        parent = parent.parentWidget()
    return widget.window()


class ScratchpadPanel(QWidget):
    def __init__(self, service: MemoryService, conversation_id: str, branch_id: str) -> None:
        super().__init__()
        self._service, self._conversation_id, self._branch_id = service, conversation_id, branch_id
        self._jobs = BackgroundTasks(self)
        self._pad = Scratchpad()
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("scratchpadEditor")
        self.editor.setAccessibleName("当前分支透明便签")
        self.editor.setPlaceholderText(
            "暂无便签。AI 可在这里整理近期待办、已确认状态和待确认事项。"
        )
        self.editor.setMinimumHeight(140)
        layout.addWidget(self.editor, 1)
        self.count = QLabel()
        layout.addWidget(self.count)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("读取最新便签")
        self.clear_button = QPushButton("清空便签")
        self.save_button = QPushButton("保存便签")
        self.save_button.setProperty("accent", True)
        row.addWidget(self.refresh_button)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.editor.textChanged.connect(self._sync)
        self.save_button.clicked.connect(self._save)
        self.refresh_button.clicked.connect(self._refresh)
        self.clear_button.clicked.connect(self._clear)
        self.reload()

    def _sync(self) -> None:
        length = len(self.editor.toPlainText())
        self.count.setText(f"{length} / {MAX_MEMORY_CHARS} 字 · 版本 {self._pad.revision}")
        self.save_button.setEnabled(not self._busy and length <= MAX_MEMORY_CHARS)
        self.clear_button.setEnabled(not self._busy and bool(self._pad.content))
        self.refresh_button.setEnabled(not self._busy)
        self.editor.setReadOnly(self._busy)

    def _loaded(self, pad: Scratchpad) -> None:
        self._pad = pad
        self._busy = False
        self.editor.setPlainText(pad.content)
        self._sync()

    def _failed(self, exc: Exception) -> None:
        self._busy = False
        self.status.setText(str(exc))
        self._sync()  # Keep the draft on conflict or storage failure.

    def reload(self) -> None:
        self._busy = True
        self.status.clear()
        self._sync()
        self._jobs.submit(
            lambda: self._service.read_scratchpad(self._conversation_id, self._branch_id),
            self._loaded, self._failed,
        )

    def _refresh(self) -> None:
        if self.editor.toPlainText() != self._pad.content:
            ask_confirm(_floating_parent(self), "放弃未保存的便签修改？", "将读取最新保存的便签。",
                        lambda yes: self.reload() if yes else None)
        else:
            self.reload()

    def _save(self, *, content: str | None = None) -> None:
        text = self.editor.toPlainText() if content is None else content
        revision = self._pad.revision
        self._busy = True
        self.status.clear()
        self._sync()

        def ready(pad: Scratchpad) -> None:
            self._loaded(pad)
            self.status.setText("便签已保存；下一轮对话生效，不会加入正式记忆。")

        self._jobs.submit(
            lambda: self._service.write_scratchpad(
                self._conversation_id, self._branch_id, text, revision,
            ), ready, self._failed,
        )

    def _clear(self) -> None:
        ask_confirm(_floating_parent(self), "清空当前分支便签？",
                    "仅清空当前便签，不影响正式记忆；已有历史分支快照保持不变。",
                    lambda yes: self._save(content="") if yes else None)


class ArchivedMemoryPanel(QWidget):
    changed = Signal()

    def __init__(self, service: MemoryService, conversation_id: str, branch_id: str) -> None:
        super().__init__()
        self._service, self._conversation_id, self._branch_id = service, conversation_id, branch_id
        self._jobs = BackgroundTasks(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.items = QListWidget()
        self.items.setAccessibleName("当前分支归档记忆")
        self.items.setMinimumHeight(70)
        layout.addWidget(self.items, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(140)
        self.preview.setAccessibleName("选中归档记忆的完整正文")
        self.preview.setPlaceholderText("暂无归档记忆。归档内容不再注入日常对话，但可以检索或恢复。")
        layout.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("刷新归档")
        self.restore_button = QPushButton("恢复为会话记忆")
        self.delete_button = QPushButton("删除归档")
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        row.addWidget(self.delete_button)
        row.addWidget(self.restore_button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.items.currentRowChanged.connect(self._select)
        self.refresh_button.clicked.connect(self.reload)
        self.restore_button.clicked.connect(self._restore)
        self.delete_button.clicked.connect(self._delete)
        self.reload()

    def _select(self, row: int) -> None:
        item = self.items.item(row)
        memory = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.preview.setPlainText(memory.content if memory else "")
        self.restore_button.setEnabled(memory is not None)
        self.delete_button.setEnabled(memory is not None)

    def _failed(self, exc: Exception) -> None:
        self.setEnabled(True)
        self.status.setText(str(exc))

    def reload(self) -> None:
        self.setEnabled(False)

        def loaded(memories: list[MemoryItem]) -> None:
            self.items.clear()
            for memory in memories:
                self.items.addItem(memory.content.splitlines()[0])
                self.items.item(self.items.count() - 1).setData(Qt.ItemDataRole.UserRole, memory)
            self._select(-1)
            self.setEnabled(True)

        self._jobs.submit(
            lambda: self._service.list_archived(self._conversation_id, self._branch_id),
            loaded, self._failed,
        )

    def _restore(self) -> None:
        self._change(delete=False)

    def _delete(self) -> None:
        item = self.items.currentItem()
        if item is None:
            return
        memory_id = item.data(Qt.ItemDataRole.UserRole).id
        ask_confirm(_floating_parent(self), "删除这条归档记忆？",
                    "将从当前分支归档中删除，历史快照仍保留。",
                    lambda yes: self._change(delete=True, memory_id=memory_id) if yes else None)

    def _change(self, *, delete: bool, memory_id: str | None = None) -> None:
        if memory_id is None:
            item = self.items.currentItem()
            if item is None:
                return
            memory_id = item.data(Qt.ItemDataRole.UserRole).id
        self.setEnabled(False)
        action = self._service.delete_archived if delete else self._service.restore_archived

        def done(_value: object) -> None:
            self.status.setText("已删除归档。" if delete else "已恢复；下一轮对话生效。")
            self.changed.emit()
            self.reload()

        self._jobs.submit(lambda: action(memory_id, self._conversation_id, self._branch_id),
                          done, self._failed)
