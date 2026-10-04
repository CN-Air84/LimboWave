"""请求日志查看器（Task 3.3 的界面）。

左侧是会话的运行列表，右侧是选中运行的**双快照**详情：
应用意图（本来打算发什么）与传输记录（Provider 实际收到什么）并列，
键级参数差异单独一节——设计计划 §四.4 的可观测化落在用户眼前。

架构约束：
- 只读视图：不修改任何数据，也不触发任何内核动作。
- 快照里的敏感头已是脱敏视图（``{"present":true,"value":"[REDACTED]"}``），
  本对话框原样展示，不尝试还原——还原在结构上就不存在。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.request_log_service import (
    REQUEST_LOG_PAGE_SIZE,
    RequestLogService,
    RunLogEntry,
    RunLogPage,
)
from limbowave.domain.run import RunStatus
from limbowave.domain.snapshots import TransportSnapshot
from limbowave.domain.stream_tape import summarize

_STATUS_TEXT = {
    RunStatus.ABORTED: "已停止",
    RunStatus.COMPLETED: "完成",
    RunStatus.RUNNING: "进行中",
    RunStatus.INTERRUPTED: "已中断",
    RunStatus.FAILED: "失败",
}


class RequestLogDialog(QDialog):
    """一个会话的请求日志。"""

    def __init__(
        self,
        service: RequestLogService,
        conversation_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._conversation_id = conversation_id
        self._offset = 0
        self._page_future: Future[RunLogPage] | None = None
        self._detail_future: Future[RunLogEntry | None] | None = None
        self._entry: RunLogEntry | None = None
        self._rendered_tabs: set[int] = set()
        self._render_iterator: Iterator[None] | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(15)
        self._poll_timer.timeout.connect(self._poll_results)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(10)
        self._render_timer.timeout.connect(self._render_batch)
        self.setWindowTitle("请求日志")
        self.resize(860, 560)

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._runs = QListWidget()
        self._runs.setUniformItemSizes(True)
        self._runs.currentItemChanged.connect(self._on_select)
        splitter.addWidget(self._runs)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._summary = QLabel("选择左侧的一轮请求查看详情")
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        self._summary.setContentsMargins(8, 8, 8, 8)
        self._summary.setProperty("hint", True)
        right_layout.addWidget(self._summary)
        self._tabs = QTabWidget()
        self._intent_tree = QTreeWidget()
        self._intent_tree.setHeaderLabels(["字段", "值"])
        self._tabs.addTab(self._intent_tree, "应用意图")
        self._transport_tree = QTreeWidget()
        self._transport_tree.setHeaderLabels(["字段", "值"])
        self._tabs.addTab(self._transport_tree, "实际传输")
        self._diff_tree = QTreeWidget()
        self._diff_tree.setHeaderLabels(["参数键", "来源"])
        self._tabs.addTab(self._diff_tree, "参数差异")
        self._response_tree = QTreeWidget()
        self._response_tree.setHeaderLabels(["字段", "值"])
        self._tabs.addTab(self._response_tree, "原始响应")
        self._tape_tree = QTreeWidget()
        self._tape_tree.setHeaderLabels(["流式事件", "详情"])
        self._tabs.addTab(self._tape_tree, "流式事件")
        self._trees = (
            self._intent_tree, self._transport_tree, self._diff_tree,
            self._response_tree, self._tape_tree,
        )
        for tree in self._trees:
            tree.setUniformRowHeights(True)
            tree.setColumnWidth(0, 240)
        self._tabs.currentChanged.connect(self._render_tab)
        right_layout.addWidget(self._tabs, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 620])
        root.addWidget(splitter, 1)

        navigation = QHBoxLayout()
        self._previous = QPushButton("上一页")
        self._next = QPushButton("下一页")
        self._reload = QPushButton("刷新 / 重试")
        self._page_status = QLabel()
        self._previous.clicked.connect(
            lambda: self._load_page(self._offset - REQUEST_LOG_PAGE_SIZE)
        )
        self._next.clicked.connect(lambda: self._load_page(self._offset + REQUEST_LOG_PAGE_SIZE))
        self._reload.clicked.connect(self.reload)
        navigation.addWidget(self._previous)
        navigation.addWidget(self._next)
        navigation.addWidget(self._page_status, 1)
        navigation.addWidget(self._reload)
        root.addLayout(navigation)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        close_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(close_box)

        self.reload()

    # ---------- 数据 ----------

    def reload(self) -> None:
        self._load_page(self._offset)

    def set_conversation(self, conversation_id: str) -> None:
        if self._conversation_id != conversation_id:
            self._conversation_id = conversation_id
            self._offset = 0
        self.reload()

    def _clear_detail(self) -> None:
        if self._detail_future is not None:
            self._detail_future.cancel()
            self._detail_future = None
        self._render_timer.stop()
        self._render_iterator = None
        self._entry = None
        self._rendered_tabs.clear()
        for tree in self._trees:
            tree.clear()

    def _load_page(self, offset: int) -> None:
        if self._page_future is not None:
            self._page_future.cancel()
            self._page_future = None
        self._offset = max(0, offset)
        self._runs.clear()
        self._clear_detail()
        self._summary.setText("正在加载请求日志…")
        self._page_status.setText("加载中…")
        self._previous.setEnabled(False)
        self._next.setEnabled(False)
        try:
            self._page_future = self._service.load_page_async(
                self._conversation_id, offset=self._offset,
            )
        except Exception:
            self._list_failed()
            return
        self._poll_timer.start()

    def _list_failed(self) -> None:
        self._page_status.setText("加载失败")
        self._summary.setText("请求日志加载失败，请点击“刷新 / 重试”。其他设置仍可正常使用。")
        self._previous.setEnabled(self._offset > 0)

    def _poll_results(self) -> None:
        # Only the latest futures are retained. Results from old conversations,
        # reloads or selections can never overwrite the current view.
        if self._page_future is not None and self._page_future.done():
            future, self._page_future = self._page_future, None
            try:
                page = future.result()
                for row in page.entries:
                    status = _STATUS_TEXT.get(row.status, row.status.value)
                    when = row.created_at.astimezone().strftime("%m-%d %H:%M:%S")
                    item = QListWidgetItem(f"{when}  {status}  · {row.transport_count} 次传输")
                    item.setData(Qt.ItemDataRole.UserRole, row.id)
                    self._runs.addItem(item)
                self._previous.setEnabled(self._offset > 0)
                self._next.setEnabled(page.has_more)
                self._page_status.setText(
                    f"第 {self._offset // REQUEST_LOG_PAGE_SIZE + 1} 页 · {len(page.entries)} 条"
                )
                self._summary.setText(
                    "选择左侧的一轮请求查看详情" if page.entries else "暂无请求日志"
                )
            except Exception:
                self._list_failed()
        if self._detail_future is not None and self._detail_future.done():
            detail, self._detail_future = self._detail_future, None
            try:
                entry = detail.result()
                if entry is None:
                    self._summary.setText("这轮请求已不存在，请刷新列表。")
                else:
                    self._render(entry)
            except Exception:
                self._summary.setText("详情加载失败，请重新选择请求或点击“刷新 / 重试”。")
        if self._page_future is None and self._detail_future is None:
            self._poll_timer.stop()

    def _on_select(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        self._clear_detail()
        if current is None:
            return
        self._summary.setText("正在加载请求详情…")
        try:
            self._detail_future = self._service.load_run_async(
                current.data(Qt.ItemDataRole.UserRole)
            )
        except Exception:
            self._summary.setText("详情加载失败，请点击“刷新 / 重试”。")
            return
        self._poll_timer.start()

    # ---------- 渲染 ----------

    def _render(self, entry: RunLogEntry) -> None:
        run = entry.run
        parts = [
            f"运行 {run.id}",
            f"状态：{_STATUS_TEXT.get(run.status, run.status.value)}",
        ]
        if run.error:
            parts.append(f"错误：{run.error}")
        if entry.intent is not None:
            parts.append(
                f"路由：{entry.intent.logical_model_id} → {entry.intent.endpoint_id}"
                f"（{entry.intent.routing_reason}）"
            )
        else:
            parts.append("（本轮没有意图快照——可能在内核调用前就已中断）")
        self._summary.setText("　".join(parts))

        self._entry = entry
        self._render_tab(self._tabs.currentIndex())

    def _render_tab(self, index: int) -> None:
        self._render_timer.stop()
        self._render_iterator = None
        if self._entry is None or index in self._rendered_tabs:
            return
        renderers = (
            self._render_intent, self._render_transports, self._render_diff,
            self._render_responses, self._render_tapes,
        )
        self._render_iterator = renderers[index](self._entry)
        self._render_timer.start()

    def _render_batch(self) -> None:
        iterator = self._render_iterator
        if iterator is None:
            self._render_timer.stop()
            return
        tree = self._trees[self._tabs.currentIndex()]
        tree.setUpdatesEnabled(False)
        deadline = time.perf_counter() + 0.006
        try:
            # Both a time budget and a row budget, including nested event loops.
            for _ in range(100):
                next(iterator)
                if time.perf_counter() >= deadline:
                    break
        except StopIteration:
            tree.expandToDepth(0)
            self._rendered_tabs.add(self._tabs.currentIndex())
            self._render_iterator = None
            self._render_timer.stop()
        except Exception:
            tree.clear()
            QTreeWidgetItem(tree, ["内容无法显示", "请重新选择请求或刷新后重试"])
            self._render_iterator = None
            self._render_timer.stop()
        finally:
            tree.setUpdatesEnabled(True)

    def _render_intent(self, entry: RunLogEntry) -> Iterator[None]:
        tree = self._intent_tree
        tree.clear()
        intent = entry.intent
        if intent is None:
            QTreeWidgetItem(tree, ["（无意图快照）", ""])
            return
        QTreeWidgetItem(tree, ["逻辑模型", intent.logical_model_id])
        QTreeWidgetItem(tree, ["目标端点", intent.endpoint_id])
        QTreeWidgetItem(tree, ["路由原因", intent.routing_reason])
        params = QTreeWidgetItem(tree, ["应用参数", ""])
        for key, value in sorted(intent.app_params.items()):
            yield
            QTreeWidgetItem(params, [key, _short(value)])
        if intent.message_ids:
            QTreeWidgetItem(tree, ["消息引用", f"{len(intent.message_ids)} 条"])

    def _render_transports(self, entry: RunLogEntry) -> Iterator[None]:
        tree = self._transport_tree
        tree.clear()
        if not entry.transports:
            QTreeWidgetItem(tree, ["（无传输记录）", ""])
            return
        for transport in entry.transports:
            yield
            top = QTreeWidgetItem(
                tree, [f"第 {transport.sequence} 次传输", _transport_status(transport)]
            )
            if transport.url:
                QTreeWidgetItem(top, ["URL", transport.url])
            headers = QTreeWidgetItem(top, ["请求头", ""])
            for key, value in sorted(transport.headers.items()):
                yield
                QTreeWidgetItem(headers, [key, _header_value(value)])
            body = QTreeWidgetItem(top, ["请求体", ""])
            for key, value in sorted(transport.body.items()):
                yield
                QTreeWidgetItem(body, [key, _short(value)])
            if transport.response_status is not None:
                QTreeWidgetItem(top, ["响应状态", str(transport.response_status)])
            if transport.stop_reason:
                QTreeWidgetItem(top, ["停止原因", transport.stop_reason])
            if transport.error_class:
                QTreeWidgetItem(top, ["错误类别", transport.error_class])

    def _render_diff(self, entry: RunLogEntry) -> Iterator[None]:
        """参数来源：意图有 / 传输有 / 两者都有。设计计划 §四.4 的用户可见面。"""
        tree = self._diff_tree
        tree.clear()
        intent_params = entry.intent.app_params if entry.intent else {}
        transport_body = entry.transports[0].body if entry.transports else {}
        diff = _diff_keys(intent_params, transport_body)
        for key in diff["shared"]:
            yield
            QTreeWidgetItem(tree, [key, "应用意图 ✓ / 实际传输 ✓"])
        for key in diff["only_in_intent"]:
            yield
            QTreeWidgetItem(tree, [key, "仅应用意图（未上线）"])
        for key in diff["only_in_transport"]:
            yield
            QTreeWidgetItem(tree, [key, "仅实际传输（运行时注入）"])
        if not any(diff.values()):
            QTreeWidgetItem(tree, ["（无可比较的参数）", ""])

    def _render_responses(self, entry: RunLogEntry) -> Iterator[None]:
        """「原始响应」（§十三.1）。**边界要说清**：这是解析后的响应对象。"""
        tree = self._response_tree
        tree.clear()
        note = QTreeWidgetItem(
            tree,
            [
                "说明",
                "这是运行时解析后的响应对象，不是 Provider 的线上原始字节——"
                "扩展只能在响应头到达时观测，拿不到 HTTP 正文。",
            ],
        )
        note.setFirstColumnSpanned(True)
        if not entry.transports:
            QTreeWidgetItem(tree, ["（无传输记录）", ""])
            return
        for transport in entry.transports:
            yield
            body = transport.response_body
            label = f"第 {transport.sequence} 次传输（尝试 {transport.attempt}）"
            if not body:
                QTreeWidgetItem(tree, [label, "（没有响应对象——本轮未收到完整回复）"])
                continue
            top = QTreeWidgetItem(tree, [label, _stop_text(body.get("stopReason"))])
            if body.get("model"):
                QTreeWidgetItem(top, ["模型", str(body["model"])])
            if body.get("stopReason"):
                QTreeWidgetItem(top, ["停止原因", str(body["stopReason"])])
            usage = body.get("usage")
            if isinstance(usage, dict) and usage:
                usage_node = QTreeWidgetItem(top, ["用量", ""])
                for key, value in sorted(usage.items()):
                    yield
                    QTreeWidgetItem(usage_node, [key, _short(value)])
            if body.get("errorMessage"):
                QTreeWidgetItem(top, ["错误消息", _short(body["errorMessage"])])
            content = body.get("content")
            if isinstance(content, list):
                blocks = QTreeWidgetItem(top, [f"内容块（{len(content)}）", ""])
                for index, block in enumerate(content):
                    yield
                    kind = block.get("type") if isinstance(block, dict) else type(block).__name__
                    QTreeWidgetItem(blocks, [f"#{index + 1} {kind}", _block_text(block)])
            if body.get("_truncated"):
                QTreeWidgetItem(
                    top,
                    [
                        "（已截断）",
                        f"原文约 {int(body.get('_original_chars') or 0)} 字符；"
                        "完整正文见对话中的消息",
                    ],
                )

    def _render_tapes(self, entry: RunLogEntry) -> Iterator[None]:
        """「流式事件」（§十三.1）：紧凑序列，看得见「怎么流的」。"""
        tree = self._tape_tree
        tree.clear()
        note = QTreeWidgetItem(
            tree,
            [
                "说明",
                "只记事件类型、时间偏移与字符数（增量文本不重复存，最终消息里有全文）。"
                "相邻事件的偏移差就是停顿位置。",
            ],
        )
        note.setFirstColumnSpanned(True)
        if not entry.transports:
            QTreeWidgetItem(tree, ["（无传输记录）", ""])
            return
        for transport in entry.transports:
            yield
            tape = transport.stream_tape
            label = f"第 {transport.sequence} 次传输（尝试 {transport.attempt}）"
            if not tape:
                QTreeWidgetItem(tree, [label, "（无流式记录）"])
                continue
            top = QTreeWidgetItem(tree, [label, summarize(tape)])
            for event in tape.get("events") or []:
                yield
                if not isinstance(event, dict):
                    continue
                QTreeWidgetItem(top, [_event_name(event), _event_detail(event)])
            dropped = int(tape.get("dropped") or 0)
            if dropped:
                QTreeWidgetItem(
                    top,
                    ["（已截断）", f"超出上限丢弃 {dropped} 条事件（计数仍完整）"],
                )


def _diff_keys(intent: dict[str, object], transport: dict[str, object]) -> dict[str, list[str]]:
    intent_keys = set(intent)
    transport_keys = set(transport)
    return {
        "shared": sorted(intent_keys & transport_keys),
        "only_in_intent": sorted(intent_keys - transport_keys),
        "only_in_transport": sorted(transport_keys - intent_keys),
    }


def _transport_status(transport: TransportSnapshot) -> str:
    if transport.error_class:
        return f"错误（{transport.error_class}）"
    if transport.response_status is not None:
        return f"HTTP {transport.response_status}"
    return "未响应"


# 事件 → 中文名。新增类型会走默认分支，界面不会因此空白。
_EVENT_NAMES = {
    "start": "开始",
    "text": "正文增量",
    "thinking": "思考增量",
    "end": "结束",
    "http": "响应头到达",
    "tool.start": "工具开始",
    "tool.end": "工具结束",
    "retry": "重试",
}


def _event_name(event: dict[str, Any]) -> str:
    kind = str(event.get("e") or "?")
    offset = int(event.get("t") or 0)
    return f"+{offset}ms  {_EVENT_NAMES.get(kind, kind)}"


def _event_detail(event: dict[str, Any]) -> str:
    parts: list[str] = []
    if event.get("n"):
        parts.append(f"{int(event['n'])} 字")
    if event.get("status") is not None:
        parts.append(f"HTTP {event['status']}")
    if event.get("stop"):
        parts.append(f"停止：{event['stop']}")
    if event.get("name"):
        parts.append(str(event["name"]))
    if event.get("error"):
        parts.append("失败")
    if event.get("attempt") is not None:
        parts.append(f"第 {event['attempt']} 次尝试")
    if event.get("klass"):
        parts.append(str(event["klass"]))
    if event.get("delay_ms") is not None:
        parts.append(f"退避 {event['delay_ms']}ms")
    if event.get("preview"):
        parts.append(_short(event["preview"], 200))
    return "，".join(parts)


def _block_text(block: object) -> str:
    """内容块的展示文本：优先正文/思考字段，工具调用显示工具名。"""
    if isinstance(block, str):
        return _short(block)
    if not isinstance(block, dict):
        return _short(block)
    for key in ("text", "thinking", "content"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return _short(value)
    if block.get("name"):
        return f"{block['name']}  {_short(block.get('input'), 120)}"
    return _short(block)


def _stop_text(stop: object) -> str:
    if not stop:
        return ""
    return f"停止原因：{stop}"


def _header_value(value: object) -> str:
    """脱敏头的结构化视图 → 用户可读文本。绝不还原密钥。"""
    if isinstance(value, dict) and "present" in value:
        if not value["present"]:
            return "（未发送）"
        scheme = value.get("scheme")
        return f"[已脱敏]{' ' + str(scheme) if scheme else ''}"
    return _short(value)


def _short(value: object, limit: int = 160) -> str:
    if isinstance(value, str):
        text = value
    else:
        # Stop encoding after the preview; a request may contain megabytes of messages.
        parts: list[str] = []
        remaining = limit + 1
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value):
            parts.append(chunk[:remaining])
            remaining -= len(parts[-1])
            if remaining <= 0:
                break
        text = "".join(parts)
    if len(text) > limit:
        return text[:limit] + "…"
    return text
