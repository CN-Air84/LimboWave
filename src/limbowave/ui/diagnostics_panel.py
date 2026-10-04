"""可嵌入的诊断分析面板。只读近期内存窗口，不扫描磁盘、不接管管理器生命周期。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, QTimer
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from limbowave.infrastructure.diagnostics import (
    LogAnalyzer,
    LogEntry,
    LogLevel,
    LogManager,
    LogQuery,
)


class DiagnosticTableModel(QAbstractTableModel):
    HEADERS = ("UTC 时间", "等级", "来源", "消息")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[LogEntry] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.HEADERS)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                entry.timestamp[11:23],
                entry.level.name,
                entry.logger,
                entry.message.replace("\n", " ↵ ")[:300],
            )[index.column()]
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def replace(self, entries: list[LogEntry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()


class DiagnosticsPanel(QWidget):
    def __init__(
        self,
        manager: LogManager,
        parent: QWidget | None = None,
        *,
        display_limit: int = 500,
        on_level_changed: Callable[[LogLevel], None] | None = None,
    ) -> None:
        if display_limit < 1:
            raise ValueError("display_limit must be positive")
        super().__init__(parent)
        self._manager = manager
        self._display_limit = display_limit
        self._snapshot: list[LogEntry] = []
        self._sequence = -1
        self.setWindowTitle("诊断日志")
        root = QVBoxLayout(self)
        collection = QHBoxLayout()
        collection.addWidget(QLabel("收集等级（本次运行）"))
        self._capture_level = QComboBox()
        self._capture_level.setObjectName("diagnosticsCaptureLevel")
        for level in LogLevel:
            self._capture_level.addItem(level.name, int(level))
        self._capture_level.setCurrentText(manager.level.name)
        change_level = on_level_changed or manager.set_level
        self._capture_level.currentIndexChanged.connect(
            lambda: change_level(LogLevel(self._capture_level.currentData()))
        )
        collection.addWidget(self._capture_level)
        self._location = QLabel(
            str(manager.config.directory / f"{manager.config.name}.jsonl")
            if manager.config.file_enabled
            else "仅内存记录：文件输出不可用或未启用"
        )
        self._location.setTextFormat(Qt.TextFormat.PlainText)
        self._location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._location.setWordWrap(True)
        collection.addWidget(self._location, 1)
        root.addLayout(collection)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("筛选最低等级"))
        self._level = QComboBox()
        for level in LogLevel:
            self._level.addItem(level.name, int(level))
        self._level.setObjectName("diagnosticsLevel")
        filters.addWidget(self._level)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索消息、异常或上下文")
        self._search.setObjectName("diagnosticsSearch")
        filters.addWidget(self._search, 2)
        self._source = QLineEdit()
        self._source.setPlaceholderText("来源，如 limbowave.pi")
        self._source.setObjectName("diagnosticsSource")
        filters.addWidget(self._source, 1)
        self._pause = QPushButton("暂停刷新")
        self._pause.setCheckable(True)
        filters.addWidget(self._pause)
        root.addLayout(filters)

        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self._table = QTableView()
        self._model = DiagnosticTableModel(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(False)
        self._table.verticalHeader().hide()
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 125)
        self._table.setColumnWidth(1, 90)
        self._table.setColumnWidth(2, 180)
        splitter.addWidget(self._table)
        tabs = QTabWidget()
        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._stats = QPlainTextEdit()
        self._stats.setReadOnly(True)
        tabs.addTab(self._details, "记录详情")
        tabs.addTab(self._stats, "辅助分析")
        splitter.addWidget(tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)
        note = QLabel("近期缓存包含未落盘及被丢弃的记录；这里只分析当前筛选窗口，不代表全部历史。")
        note.setWordWrap(True)
        note.setProperty("hint", True)
        root.addWidget(note)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._apply_filters)
        self._search.textChanged.connect(lambda: self._debounce.start())
        self._source.textChanged.connect(lambda: self._debounce.start())
        self._level.currentIndexChanged.connect(self._apply_filters)
        self._pause.toggled.connect(self._toggle_pause)
        self._table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.refresh(force=True)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._pause.isChecked():
            self._timer.start()
            self.refresh()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        self._debounce.stop()
        super().hideEvent(event)

    def _toggle_pause(self, paused: bool) -> None:
        self._pause.setText("继续刷新" if paused else "暂停刷新")
        if paused:
            self._timer.stop()
        else:
            if self.isVisible():
                self._timer.start()
            self.refresh(force=True)

    def _apply_filters(self) -> None:
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        if self._pause.isChecked() and not force:
            return
        status = self._manager.status()
        if self._capture_level.currentText() != self._manager.level.name:
            self._capture_level.blockSignals(True)
            self._capture_level.setCurrentText(self._manager.level.name)
            self._capture_level.blockSignals(False)
        lost = status.queue_dropped + status.rate_limited + status.file_dropped
        parts = [
            f"{status.state} · 已写盘 {status.written} · 待处理 {status.pending}",
            f"丢弃 {lost}（队列 {status.queue_dropped} / 限流 {status.rate_limited}"
            f" / 文件故障 {status.file_dropped}）· 格式错误 {status.format_errors}",
        ]
        if not self._manager.config.file_enabled:
            parts.append(f"仅内存模式：{status.file_skipped} 条未启用文件保存，退出后不会保留")
        if status.last_file_error:
            parts.append("写盘异常：" + status.last_file_error)
        if status.last_console_error:
            parts.append("控制台异常：" + status.last_console_error)
        self._status.setText("\n".join(parts))
        changed = status.sequence != self._sequence
        if not self._pause.isChecked() and changed:
            self._snapshot = self._manager.recent(limit=self._manager.config.recent_capacity)
            self._sequence = status.sequence
        if not changed and not force:
            return
        query = LogQuery(
            min_level=self._level.currentData(),
            text=self._search.text(),
            logger=self._source.text().strip(),
        )
        entries = [entry for entry in self._snapshot if query.matches(entry)][
            -self._display_limit :
        ]
        current = self._table.currentIndex()
        selected = (
            self._model.entries[current.row()].sequence
            if current.isValid() and current.row() < len(self._model.entries)
            else None
        )
        self._model.replace(entries)
        if entries:
            row = next(
                (i for i, entry in enumerate(entries) if entry.sequence == selected),
                len(entries) - 1,
            )
            self._table.selectRow(row)
        else:
            self._details.clear()
        summary = LogAnalyzer.summarize(entries)
        lines = [
            f"当前显示 {summary.total} 条；缓存上限 {self._manager.config.recent_capacity}；"
            f"显示上限 {self._display_limit}",
            f"UTC 范围：{summary.first_timestamp or '—'} → {summary.last_timestamp or '—'}",
            "\n等级分布",
            *[f"  {level}: {count}" for level, count in summary.by_level.items()],
            "\n主要来源",
            *[f"  {source}: {count}" for source, count in summary.by_logger.items()],
            "\n异常类型",
            *[f"  {kind}: {count}" for kind, count in summary.by_exception.items()],
            "\n重复警告/错误（按等级、来源、消息及异常类型精确分组）",
            *[
                f"  {group.count} 次 · {group.level.name} · {group.logger}: {group.message}"
                for group in summary.repeated_problems
            ],
        ]
        if summary.groups_truncated:
            lines.append("分组上限已达到，部分分组未保留。")
        self._stats.setPlainText("\n".join(lines))

    def _selection_changed(self) -> None:
        index = self._table.currentIndex()
        if index.isValid() and index.row() < len(self._model.entries):
            entry = self._model.entries[index.row()]
            self._details.setPlainText(json.dumps(entry.to_dict(), ensure_ascii=False, indent=2))
