"""§十三.1 的「原始响应」与「流式事件」（请求日志最后两项）。

三条值得单独说清的约定，测试逐条盯住：

1. **归集锚点是 provider 请求序号**，不是助手消息序号——一次 run 里重试与工具
   往返都会新增请求，只有请求序号能把「这段流是谁吐的」说清楚。
2. **「原始响应」是解析后的响应对象**，不是线上字节（Pi 不给扩展 HTTP 正文）。
   测试断言的是「该有的字段都在」，而不是假装存了原文。
3. **脱敏在落库前完成**：模型回显的凭据也不能进日志（§十三.1 明文要求
   「加密原始日志保存完整凭据应默认禁止」）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from limbowave.application.kernel import (
    AgentKernel,
    CompactionResult,
    EventHandler,
    KernelCapabilities,
    KernelEvent,
    KernelState,
    PermissionHandler,
)
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.stream_tape import MAX_EVENTS, StreamTape, summarize
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.migrations import CURRENT_VERSION, migrate
from limbowave.infrastructure.database.sqlite_repositories import (
    open_connection,
    sqlite_uow_factory,
)
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

SECRET = "sk-live-RESPONSE-MUST-BE-REDACTED"


class FakeKernel(AgentKernel):
    """可编程假内核：直接注入内核事件与 provider 观测。"""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._observers: list[Callable[[dict[str, Any]], None]] = []

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities()

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def send_message(
        self, text: str, *, images: list[dict[str, Any]] | None = None
    ) -> None: ...

    async def abort(self) -> None: ...

    async def get_state(self) -> KernelState:
        return KernelState(
            model_id="fake",
            thinking_level="off",
            is_streaming=False,
            is_compacting=False,
            session_id="s",
            session_name=None,
            message_count=0,
            pending_message_count=0,
        )

    async def set_model(self, provider: str, model_id: str) -> None: ...

    async def set_thinking_level(self, level: str) -> None: ...

    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        return []

    async def fork(self, entry_id: str) -> str:
        return ""

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult:
        return CompactionResult(summary="", tokens_before=0)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: None

    async def events(self) -> AsyncIterator[KernelEvent]:
        return
        yield  # pragma: no cover

    def set_permission_handler(self, handler: PermissionHandler | None) -> None: ...

    def set_observation_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        if handler is not None:
            self._observers.append(handler)

    # --- 测试助手 ---

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            handler(KernelEvent(kind=kind, payload=payload))

    def observe(self, payload: dict[str, Any]) -> None:
        for observer in list(self._observers):
            observer(payload)

    def request(self, *, status: int = 200) -> None:
        """一次 provider 请求 + 响应头（应用就是靠它划定归属边界）。"""
        self.observe({"kind": "provider.request", "payload": {"model": "m", "messages": []}})
        self.observe({"kind": "provider.response", "status": status, "headers": {}})

    def settle(self) -> None:
        """本轮安定 → 触发落库（传输快照就是在这一步写的）。"""
        self.emit("run.settled", {})

    def stream(self, deltas: list[str], *, thinking: list[str] | None = None) -> None:
        """模拟一段流：思考增量在前，正文增量在后。"""
        self.emit("message.start", {"message": {"role": "assistant"}})
        for chunk in thinking or []:
            self.emit(
                "message.update",
                {"assistantMessageEvent": {"type": "thinking_delta", "delta": chunk}},
            )
        for chunk in deltas:
            self.emit(
                "message.update",
                {"assistantMessageEvent": {"type": "text_delta", "delta": chunk}},
            )

    def finish(
        self,
        text: str,
        *,
        stop: str = "stop",
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        extra_content: list[dict[str, Any]] | None = None,
        unknown_event: dict[str, Any] | None = None,
    ) -> None:
        if unknown_event is not None:
            self.emit("message.update", {"assistantMessageEvent": unknown_event})
        message: dict[str, Any] = {
            "role": "assistant",
            "model": "mock-model-1",
            "content": [{"type": "text", "text": text}, *(extra_content or [])],
            "stopReason": stop,
        }
        if usage is not None:
            message["usage"] = usage
        if error is not None:
            message["errorMessage"] = error
        self.emit("message.end", {"message": message})


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def _coordinator(kernel: FakeKernel, store: InMemoryStore) -> RunCoordinator:
    context = RunContext(
        logical_model_id="logical-a",
        endpoint_id="endpoint-a",
        routing_reason="默认绑定",
        app_params={"model": "m"},
    )
    # 构造函数**已经**订阅内核事件与观测，测试不要再接一遍（重复接线会让每
    # 个事件被投递两次，传输快照会翻倍）
    return RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: context)


def _transports(store: InMemoryStore, run_id: str) -> list[Any]:
    rows = [t for t in store.transports.values() if t.run_id == run_id]
    return sorted(rows, key=lambda t: t.sequence)


# ---------- StreamTape 本身 ----------


def test_tape_aggregates_counts_and_offsets() -> None:
    tape = StreamTape()
    tape.add("start", offset_ms=0)
    tape.add("thinking", offset_ms=10, chars=5)
    tape.add("text", offset_ms=120, chars=3)
    tape.add("text", offset_ms=140, chars=7)
    tape.add("end", offset_ms=200, stop="stop", chars=10)

    data = tape.to_dict()
    assert data["event_count"] == 5
    assert data["text_chars"] == 10
    assert data["thinking_chars"] == 5
    assert data["dropped"] == 0
    assert [e["e"] for e in data["events"]] == ["start", "thinking", "text", "text", "end"]
    assert [e["t"] for e in data["events"]] == [0, 10, 120, 140, 200]
    assert data["events"][-1]["stop"] == "stop"


def test_tape_truncates_events_but_keeps_counters() -> None:
    """超上限只截事件，计数仍完整——否则「流了多少字」会变成谎话。"""
    tape = StreamTape()
    for _ in range(MAX_EVENTS + 25):
        tape.add("text", offset_ms=1, chars=2)

    data = tape.to_dict()
    assert len(data["events"]) == MAX_EVENTS
    assert data["dropped"] == 25
    assert data["event_count"] == MAX_EVENTS + 25
    assert data["text_chars"] == (MAX_EVENTS + 25) * 2
    assert "截断丢弃 25" in summarize(data)


def test_tape_keeps_unknown_event_types_with_truncated_preview() -> None:
    """Pi 会加事件类型：没见过的类型留预览，不静默丢。"""
    tape = StreamTape()
    tape.add_unknown("citation_delta", offset_ms=7, preview={"x": "y" * 5000})

    event = tape.to_dict()["events"][0]
    assert event["e"] == "citation_delta"
    assert len(event["preview"]) <= 240


def test_tape_summarize_on_empty() -> None:
    assert summarize({}) == "无流式记录"
    assert summarize(None) == "无流式记录"
    assert summarize(StreamTape().to_dict()) == "无流式记录"


# ---------- 协调器：归集到正确的 provider 请求 ----------


async def test_stream_tape_and_response_are_attached_to_single_request(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """一次请求：磁带与响应对象落在同一条传输快照上。"""
    coord = _coordinator(kernel, store)
    run_id = await coord.send("你好")
    assert run_id is not None

    kernel.request()
    kernel.stream(["你", "好"], thinking=["想一下"])
    kernel.finish(
        "你好",
        usage={"input": 11, "output": 4},
        extra_content=[{"type": "toolCall", "name": "read_file", "input": {"path": "a.txt"}}],
    )
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    transports = _transports(store, run_id)
    assert len(transports) == 1
    transport = transports[0]

    tape = transport.stream_tape
    assert tape["event_count"] >= 5  # http + start + thinking + 2×text + end
    assert tape["text_chars"] == 2
    assert tape["thinking_chars"] == 3
    kinds = [e["e"] for e in tape["events"]]
    assert kinds[0] == "http"
    assert "thinking" in kinds and "end" in kinds
    # 思考排在正文之前（先想后说）
    assert kinds.index("thinking") < kinds.index("text")

    body = transport.response_body
    assert body["model"] == "mock-model-1"
    assert body["stopReason"] == "stop"
    assert body["usage"] == {"input": 11, "output": 4}
    assert [b["type"] for b in body["content"]] == ["text", "toolCall"]


async def test_two_requests_get_separate_tapes(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """工具往返（两次请求）各带各的磁带，不串到一条上。"""
    coord = _coordinator(kernel, store)
    run_id = await coord.send("读文件")
    assert run_id is not None

    kernel.request()
    kernel.stream(["先看"])
    kernel.finish("先看", extra_content=[{"type": "toolCall", "name": "read_file"}])
    # 工具往返后的第二次请求
    kernel.request()
    kernel.stream(["看完了"])
    kernel.finish("看完了", stop="toolUse")
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    transports = _transports(store, run_id)
    assert len(transports) == 2
    first, second = transports
    assert first.stream_tape["text_chars"] == 2
    assert second.stream_tape["text_chars"] == 3
    assert first.response_body["stopReason"] == "stop"
    assert second.response_body["stopReason"] == "toolUse"
    assert first.stream_tape["events"] != second.stream_tape["events"]


async def test_retry_marks_failed_request_and_new_attempt_gets_own_tape(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """重试：失败那次的磁带记下重试标记，新尝试另起一条磁带。"""
    context = RunContext(
        logical_model_id="logical-a",
        endpoint_id="endpoint-a",
        routing_reason="默认绑定",
        app_params={"model": "m"},
    )
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: context)
    coord._retry_policy = lambda: __import__(  # type: ignore[method-assign]
        "limbowave.domain.retry", fromlist=["RetryPolicy"]
    ).RetryPolicy(max_attempts=2, base_delay_ms=0)

    run_id = await coord.send("你好")
    assert run_id is not None

    # 第一次：建连失败（连接类错误 → 可重试）
    kernel.request(status=0)
    kernel.emit("message.end", {"message": {"role": "assistant", "stopReason": "error"}})
    kernel.observe({"kind": "provider.response", "status": 0, "headers": {}})
    coord._live.error = "connect ECONNRESET"  # 模拟内核上报的连接类错误
    coord._live.stop_reason = "error"
    coord._schedule_retry(coord._live)

    # 第二次：成功
    kernel.request()
    kernel.stream(["成了"])
    kernel.finish("成了")
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    transports = _transports(store, run_id)
    assert len(transports) >= 2
    first = transports[0]
    kinds = [e["e"] for e in first.stream_tape["events"]]
    assert "retry" in kinds, f"失败那次应留下重试标记：{kinds}"
    retry_event = next(e for e in first.stream_tape["events"] if e["e"] == "retry")
    assert retry_event["attempt"] == 1
    last = transports[-1]
    assert "retry" not in [e["e"] for e in last.stream_tape["events"]]
    assert last.stream_tape["text_chars"] == 2


async def test_tool_events_are_placed_in_the_tape(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """工具在流的哪个位置插入，磁带上有痕迹。"""
    coord = _coordinator(kernel, store)
    run_id = await coord.send("读文件")
    assert run_id is not None

    kernel.request()
    kernel.stream(["调用工具"])
    kernel.emit(
        "tool.start",
        {"toolCallId": "c1", "toolName": "read_file", "args": {"path": "a.txt"}},
    )
    kernel.emit(
        "tool.end",
        {"toolCallId": "c1", "toolName": "read_file", "result": {"ok": True}, "isError": False},
    )
    kernel.finish("调用工具")
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    tape = _transports(store, run_id)[0].stream_tape
    events = {e["e"]: e for e in tape["events"] if e["e"].startswith("tool.")}
    assert events["tool.start"]["name"] == "read_file"
    assert events["tool.end"]["name"] == "read_file"
    kinds = [e["e"] for e in tape["events"]]
    assert kinds.index("tool.start") < kinds.index("tool.end")


# ---------- 脱敏与截断 ----------


async def test_response_body_is_redacted_before_storage(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """模型回显凭据也不能进日志（§十三.1：加密日志默认不存完整凭据）。"""
    coord = _coordinator(kernel, store)
    run_id = await coord.send("你的 key 是什么")
    assert run_id is not None

    kernel.request()
    kernel.stream(["你的 key 是 "])
    kernel.finish(f"你的 key 是 {SECRET}", error=f"upstream rejected {SECRET}")
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    body = _transports(store, run_id)[0].response_body
    blob = json.dumps(body, ensure_ascii=False)
    assert SECRET not in blob, "解析后的响应里不允许留完整凭据"
    assert "[REDACTED]" in blob


async def test_oversized_response_is_truncated_with_marker(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """超长正文截断并标记——不假装日志存了全文。"""
    from limbowave.application.services.run_coordinator import MAX_RESPONSE_CHARS

    coord = _coordinator(kernel, store)
    run_id = await coord.send("写长文")
    assert run_id is not None

    huge = "字" * (MAX_RESPONSE_CHARS + 5000)
    kernel.request()
    kernel.stream(["…"])
    kernel.finish(huge)
    kernel.settle()
    kernel.settle()
    await coord.wait_idle()

    body = _transports(store, run_id)[0].response_body
    assert body.get("_truncated") is True
    assert body["_original_chars"] > MAX_RESPONSE_CHARS
    assert len(json.dumps(body, ensure_ascii=False)) < MAX_RESPONSE_CHARS * 2
    assert "截断" in json.dumps(body["content"], ensure_ascii=False)


# ---------- 落库与迁移 ----------


def test_sqlite_round_trip_encrypts_response_and_tape(
    tmp_path: Path, make_key: Callable[[], VaultKey]
) -> None:
    """新字段落库为密文，读回一字不差。"""
    from limbowave.domain.snapshots import TransportSnapshot

    key = make_key()
    db_path = tmp_path / "log.db"
    factory = sqlite_uow_factory(db_path, key)  # 工厂创建时已跑迁移

    from datetime import UTC, datetime

    from limbowave.domain.conversation import Message, MessageRole

    with factory() as uow:
        uow.conversations.add(_conversation())
        uow.branches.add(_branch())
        # runs.user_message_id 有外键，消息必须先存在
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="hi",
                created_at=datetime.now(UTC),
            )
        )
        uow.runs.add(_run())
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t1",
                run_id="r1",
                created_at=datetime.now(UTC),
                sequence=1,
                response_body={"content": [{"type": "text", "text": SECRET}]},
                stream_tape={"events": [{"e": "text", "t": 5, "n": 3}], "text_chars": 3},
            )
        )
        uow.commit()

    # 密文校验：库里不能出现明文
    with open_connection(db_path) as conn:
        raw = conn.execute(
            "SELECT response_body_enc, stream_tape_enc FROM transport_snapshots"
        ).fetchone()
    assert raw is not None
    assert SECRET not in raw[0]
    assert "text_chars" not in raw[1]

    with factory() as uow:
        loaded = uow.snapshots.list_transport("r1")
    assert len(loaded) == 1
    assert loaded[0].response_body["content"][0]["text"] == SECRET
    assert loaded[0].stream_tape["text_chars"] == 3


def test_migration_from_v5_adds_columns_with_defaults(tmp_path: Path) -> None:
    """老库升到 v6：列存在且默认空对象，老行不会炸。"""
    conn = open_connection(tmp_path / "old.db")
    migrate(conn, target=5)
    assert conn.execute(
        "SELECT COUNT(*) FROM pragma_table_info('transport_snapshots') "
        "WHERE name IN ('response_body_enc','stream_tape_enc')"
    ).fetchone()[0] == 0

    migrate(conn, target=CURRENT_VERSION)
    names = {
        row[1] for row in conn.execute("PRAGMA table_info(transport_snapshots)").fetchall()
    }
    assert {"response_body_enc", "stream_tape_enc"} <= names
    conn.execute(
        "INSERT INTO conversations (id, title_enc, created_at) VALUES ('c','x','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO branches (id, conversation_id, created_at) VALUES ('b','c','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO messages "
        "(id, conversation_id, branch_id, role, content_enc, status, created_at)"
        " VALUES ('m0','c','b','user','x','complete','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO runs (id, conversation_id, branch_id, status, created_at, user_message_id) "
        "VALUES ('r','c','b','completed','2026-01-01','m0')"
    )
    conn.execute(
        "INSERT INTO transport_snapshots (id, run_id, created_at, sequence) "
        "VALUES ('t','r','2026-01-01',1)"
    )
    row = conn.execute(
        "SELECT response_body_enc, stream_tape_enc FROM transport_snapshots WHERE id='t'"
    ).fetchone()
    assert row == ("{}", "{}")
    conn.close()


# ---------- 界面 ----------


def test_dialog_renders_new_tabs(qtbot: Any, store: InMemoryStore, tmp_path: Path) -> None:
    """两个新页签能渲染（含说明文字），且不还原任何密钥。"""
    from datetime import UTC, datetime

    from PySide6.QtWidgets import QTabWidget

    from limbowave.application.services.request_log_service import RequestLogService
    from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
    from limbowave.domain.run import RunRecord, RunStatus
    from limbowave.domain.snapshots import TransportSnapshot
    from limbowave.ui.request_log_dialog import RequestLogDialog

    now = datetime.now(UTC)
    with in_memory_uow_factory(store)() as uow:
        uow.conversations.add(Conversation(id="c1", title="会话", created_at=now))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=now))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="hi",
                created_at=now,
            )
        )
        uow.runs.add(
            RunRecord(
                id="r1",
                conversation_id="c1",
                branch_id="b1",
                status=RunStatus.COMPLETED,
                created_at=now,
                user_message_id="m1",
            )
        )
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t1",
                run_id="r1",
                created_at=now,
                sequence=1,
                response_body={
                    "model": "mock-model-1",
                    "stopReason": "stop",
                    "usage": {"input": 3, "output": 5},
                    "content": [{"type": "text", "text": "回答"}],
                    # 存进来的就已经是脱敏后的值（脱敏发生在协调器，另有专门测试）
                    "errorMessage": "boom [REDACTED]",
                },
                stream_tape={
                    "events": [
                        {"e": "http", "t": 1, "status": 200},
                        {"e": "text", "t": 20, "n": 2},
                        {
                            "e": "retry",
                            "t": 90,
                            "attempt": 1,
                            "klass": "connection",
                            "delay_ms": 500,
                        },
                    ],
                    "text_chars": 2,
                    "thinking_chars": 0,
                    "event_count": 3,
                    "dropped": 0,
                },
            )
        )
        uow.commit()

    service = RequestLogService(in_memory_uow_factory(store))
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    dialog._runs.setCurrentRow(0)
    qtbot.waitUntil(lambda: dialog._entry is not None)
    service.close()

    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None
    titles = [tabs.tabText(i) for i in range(tabs.count())]
    assert "原始响应" in titles
    assert "流式事件" in titles

    dialog._tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: 3 in dialog._rendered_tabs)
    response_text = _tree_text(dialog._response_tree)
    assert "mock-model-1" in response_text
    assert "回答" in response_text
    # 脱敏后的值原样展示，界面不做任何还原
    assert "[REDACTED]" in response_text
    assert SECRET not in response_text

    dialog._tabs.setCurrentIndex(4)
    qtbot.waitUntil(lambda: 4 in dialog._rendered_tabs)
    tape_text = _tree_text(dialog._tape_tree)
    assert "+20ms" in tape_text
    assert "+90ms" in tape_text
    assert "退避 500ms" in tape_text
    assert "3 个事件" in tape_text


def _tree_text(tree: Any) -> str:
    parts: list[str] = []
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        parts.append(f"{item.text(0)} {item.text(1)}")
        stack.extend(item.child(i) for i in range(item.childCount()))
    return "\n".join(parts)


# ---------- 辅助构造 ----------


def _conversation() -> Any:
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Conversation

    return Conversation(id="c1", title="会话", created_at=datetime.now(UTC))


def _branch() -> Any:
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Branch

    return Branch(id="b1", conversation_id="c1", created_at=datetime.now(UTC))


def _run() -> Any:
    from datetime import UTC, datetime

    from limbowave.domain.run import RunRecord, RunStatus

    return RunRecord(
        id="r1",
        conversation_id="c1",
        branch_id="b1",
        status=RunStatus.COMPLETED,
        created_at=datetime.now(UTC),
        user_message_id="m1",
    )
