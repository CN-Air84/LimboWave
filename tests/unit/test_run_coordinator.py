"""RunCoordinator 的门禁测试（Phase 1B 验收 1–8）。

不依赖真实进程：用 FakeKernel 注入内核事件与 provider 观测，
用内存仓库读回应用权威数据。真实 Pi 的纵向验证在
``tests/integration/test_run_coordinator_e2e.py``。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest

from limbowave.application.kernel import (
    AgentKernel,
    CompactionResult,
    EventHandler,
    KernelCapabilities,
    KernelCapability,
    KernelEvent,
    KernelState,
    PermissionHandler,
)
from limbowave.application.services.attachment_service import AttachmentPayload
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.conversation import MessageRole, MessageStatus
from limbowave.domain.run import RunStatus
from limbowave.domain.tool_step import ToolStatus
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

SECRET = "sk-live-MUST-NOT-BE-PERSISTED"


class FakeKernel(AgentKernel):
    """可编程假内核：记录命令，允许测试直接注入事件与观测。"""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._observers: list[Callable[[dict[str, Any]], None]] = []
        self.sent: list[str] = []
        self.model_id = "fake"
        self.provider = "fake"
        self.thinking_level = "off"
        self.sent_images: list[list[dict[str, Any]] | None] = []
        self.aborted = False
        self.entries: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.permission_handler: PermissionHandler | None = None

    # --- AgentKernel ---

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities()

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)
        self.sent_images.append(images)

    async def new_session(self) -> None:
        self.entries = []

    async def abort(self) -> None:
        self.aborted = True

    async def get_state(self) -> KernelState:
        return KernelState(
            model_id=self.model_id,
            provider=self.provider,
            thinking_level=self.thinking_level,
            is_streaming=False,
            is_compacting=False,
            session_id="fake",
            session_name=None,
            message_count=0,
            pending_message_count=0,
        )

    async def set_model(self, provider: str, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id

    async def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        return list(self.entries)

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

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self.permission_handler = handler

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

    def say(self, text: str, *, stop: str = "stop") -> None:
        """模拟一轮完整的助手回复。"""
        self.emit("message.start", {"message": {"role": "assistant"}})
        self.emit(
            "message.update",
            {"assistantMessageEvent": {"type": "text_delta", "delta": text}},
        )
        self.emit(
            "message.end",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stopReason": stop,
                }
            },
        )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


@pytest.fixture
def coordinator(kernel: FakeKernel, store: InMemoryStore) -> RunCoordinator:
    context = RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat", "max_tokens": 4096},
    )
    return RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: context)


def _messages(store: InMemoryStore) -> list[Any]:
    return sorted(store.messages.values(), key=lambda m: (m.created_at, m.id))


def _only_run(store: InMemoryStore) -> Any:
    assert len(store.runs) == 1
    return next(iter(store.runs.values()))


async def test_start_marks_runs_left_running_by_previous_process_as_interrupted(
    coordinator: RunCoordinator, store: InMemoryStore
) -> None:
    from datetime import UTC, datetime

    from limbowave.domain.run import RunRecord

    created = datetime(2026, 9, 27, 3, 52, tzinfo=UTC)
    store.runs["stale"] = RunRecord(
        id="stale",
        conversation_id="conversation",
        branch_id="branch",
        status=RunStatus.RUNNING,
        created_at=created,
        user_message_id="user-message",
    )

    await coordinator.start()

    run = store.runs["stale"]
    assert run.status is RunStatus.INTERRUPTED
    assert run.finished_at is not None
    assert run.stop_reason == "runtime_restart"
    assert run.error == "应用上次退出时运行未完成"


# ---------------------------------------------------------------- 门禁 1


async def test_gate1_normal_completion_links_everything_to_one_run(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """正常完成：用户消息、助手消息、两类快照与 Runtime 镜像同属一个 Run。"""
    kernel.entries = [
        {
            "id": "e1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "你好"},
        },
        {
            "id": "e2",
            "type": "message",
            "parentId": "e1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]},
        },
    ]
    run_id = await coordinator.send("你好")
    assert run_id is not None

    kernel.observe({"kind": "provider.request", "payload": {"model": "deepseek-chat"}})
    kernel.observe({"kind": "provider.response", "status": 200, "headers": {}})
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    run = _only_run(store)
    assert run.id == run_id
    assert run.status is RunStatus.COMPLETED

    msgs = _messages(store)
    assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert all(m.run_id == run_id for m in msgs)

    intent = next(iter(store.intents.values()))
    assert intent.run_id == run_id
    assert intent.logical_model_id == "deepseek-chat"
    assert intent.endpoint_id == "relay-a"
    assert intent.routing_reason == "默认绑定"
    assert intent.message_ids[-1] == msgs[0].id

    assert len(store.transports) == 1
    assert next(iter(store.transports.values())).run_id == run_id
    assert all(m.run_id == run_id for m in store.mirrors.values())
    assert run.assistant_message_id == msgs[1].id


# ---------------------------------------------------------------- 门禁 2


async def test_gate2_streaming_deltas_yield_single_message(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """流式输出：多个 delta 只生成一条最终助手消息。"""
    await coordinator.send("数到三")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    for piece in ("一", "二", "三"):
        kernel.emit(
            "message.update", {"assistantMessageEvent": {"type": "text_delta", "delta": piece}}
        )
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "一二三"}],
                "stopReason": "stop",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    assistants = [m for m in store.messages.values() if m.role is MessageRole.ASSISTANT]
    assert len(assistants) == 1
    assert assistants[0].content == "一二三"


async def test_only_new_conversation_first_reply_emits_auto_title_event(
    coordinator: RunCoordinator, kernel: FakeKernel
) -> None:
    events: list[Any] = []
    coordinator.subscribe(events.append)

    await coordinator.send("第一问")
    kernel.say("第一答")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    await coordinator.send("第二问")
    kernel.say("第二答")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    completed = [event for event in events if event.kind == "first_response_completed"]
    assert len(completed) == 1
    assert completed[0].data == {
        "conversation_id": coordinator.conversation_id,
        "user_text": "第一问",
        "assistant_text": "第一答",
    }
    assert all(event.data == {} for event in events if event.kind == "settled")


# ---------------------------------------------------------------- 门禁 3


async def test_gate3_user_abort_keeps_partial_output(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """用户停止：保留部分输出，Run 落 aborted。"""
    await coordinator.send("写一篇长文")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit(
        "message.update",
        {"assistantMessageEvent": {"type": "text_delta", "delta": "开头"}},
    )
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "开头"}],
                "stopReason": "aborted",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    run = _only_run(store)
    assert run.status is RunStatus.ABORTED
    assistant = next(m for m in store.messages.values() if m.role is MessageRole.ASSISTANT)
    assert assistant.content == "开头"
    assert assistant.status is MessageStatus.PARTIAL
    assert not coordinator.busy


# ---------------------------------------------------------------- 门禁 4


async def test_gate4_provider_failure_keeps_user_message_without_fake_reply(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """Provider 失败：用户消息与失败 Run 保留，且不伪造助手成功消息。"""
    await coordinator.send("触发失败")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "rate limited",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    run = _only_run(store)
    assert run.status is RunStatus.FAILED
    assert run.error == "rate limited"

    msgs = _messages(store)
    assert [m.role for m in msgs] == [MessageRole.USER]
    assert run.assistant_message_id is None


async def test_gate4_send_failure_still_records_run(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """内核启动失败也不能丢掉用户输入——这是明确锁定过的原则。"""
    kernel.send_error = RuntimeError("Pi 进程未运行")

    run_id = await coordinator.send("请记住这句话")
    await coordinator.wait_idle()

    run = _only_run(store)
    assert run.id == run_id
    assert run.status is RunStatus.FAILED
    assert "Pi 进程未运行" in (run.error or "")
    assert any(m.content == "请记住这句话" for m in store.messages.values())


# ---------------------------------------------------------------- 门禁 5


async def test_gate5_runtime_death_marks_interrupted_without_resend(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """Runtime 异常退出：本轮落 interrupted，busy 清除，且不自动重发。"""
    await coordinator.send("第一句")
    assert coordinator.busy

    await coordinator.mark_interrupted("Pi 进程异常退出")

    assert not coordinator.busy
    run = _only_run(store)
    assert run.status is RunStatus.INTERRUPTED
    assert kernel.sent == ["第一句"], "不得自动重发上一轮请求"


async def test_gate5_runtime_exited_event_also_interrupts(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """适配器上报 runtime.exited 时同样收敛为 interrupted。"""
    await coordinator.send("第一句")
    kernel.emit("runtime.exited", {"returncode": 1})
    await coordinator.wait_idle()

    assert _only_run(store).status is RunStatus.INTERRUPTED
    assert not coordinator.busy


# ---------------------------------------------------------------- 门禁 6


async def test_gate6_duplicate_terminal_events_are_idempotent(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """重复的 message_end / settled 不得产生两条助手消息或重复快照。"""
    settled_events: list[str] = []
    coordinator.subscribe(lambda e: settled_events.append(e.kind))

    await coordinator.send("只回一次")
    kernel.observe({"kind": "provider.request", "payload": {"model": "deepseek-chat"}})
    for _ in range(3):
        kernel.say("只回一次")
        kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    assistants = [m for m in store.messages.values() if m.role is MessageRole.ASSISTANT]
    assert len(assistants) == 1
    assert len(store.intents) == 1
    assert len(store.transports) == 1
    assert settled_events.count("settled") == 1


# ---------------------------------------------------------------- 门禁 7


async def test_gate7_secrets_never_persisted(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """Authorization、API key 与自定义敏感 header 永不进入持久化数据。"""
    await coordinator.send("看看请求头")
    kernel.observe(
        {
            "kind": "provider.headers",
            "headers": {
                "Authorization": f"Bearer {SECRET}",
                "X-Api-Key": SECRET,
                "X-Tenant": "demo",
            },
        }
    )
    kernel.observe(
        {
            "kind": "provider.request",
            "payload": {"model": "deepseek-chat", "api_key": SECRET, "messages": []},
        }
    )
    kernel.say("好的")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    transport = next(iter(store.transports.values()))
    assert transport.headers["Authorization"] == {
        "present": True,
        "scheme": "Bearer",
        "value": "[REDACTED]",
    }
    assert transport.headers["X-Api-Key"]["value"] == "[REDACTED]"
    assert transport.headers["X-Tenant"] == "demo"  # 非敏感头保留，便于排错
    assert transport.body["api_key"] == "[REDACTED]"

    # 全量扫描：不得出现明文密钥，也不得出现它的哈希（含截断形式）——
    # 保存"可逆或可爆破的指纹"同样是不允许的。
    blob = repr(store.__dict__)
    assert SECRET not in blob
    digest = hashlib.sha256(SECRET.encode("utf-8")).hexdigest()
    assert digest not in blob
    assert digest[:16] not in blob
    assert digest[:8] not in blob


async def test_gate7_error_text_is_scrubbed(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """异常文本里的密钥同样必须被抹掉。"""
    await coordinator.send("触发错误")
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": f"401 unauthorized: Bearer {SECRET}",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    run = _only_run(store)
    assert SECRET not in (run.error or "")
    assert "[REDACTED]" in (run.error or "")


# ---------------------------------------------------------------- 门禁 8


async def test_gate8_intent_and_transport_are_recorded_separately(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """两个快照分别记录应用意图与最终请求：Pi 注入的字段只出现在传输侧。"""
    await coordinator.send("对比两个快照")
    kernel.observe(
        {
            "kind": "provider.request",
            "payload": {
                # 应用侧参数
                "model": "deepseek-chat",
                "max_tokens": 4096,
                # Pi 自己补的字段——意图快照里没有
                "stream": True,
                "stream_options": {"include_usage": True},
                "store": False,
                "max_completion_tokens": 4096,
                "messages": [{"role": "system", "content": "You are..."}],
            },
        }
    )
    kernel.say("好")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    intent = next(iter(store.intents.values()))
    transport = next(iter(store.transports.values()))

    assert intent.app_params["model"] == "deepseek-chat"
    assert "stream" not in intent.app_params

    diff = transport.param_diff
    assert "stream" in diff["only_in_transport"]
    assert "max_completion_tokens" in diff["only_in_transport"]
    assert "messages" in diff["only_in_transport"]
    assert diff["only_in_intent"] == []
    assert set(diff["shared"]) == {"model", "max_tokens"}
    # 传输快照确实保留了最终请求体
    assert transport.body["max_completion_tokens"] == 4096


# ---------------------------------------------------------------- 附加：镜像与主键区分


async def test_pi_entry_id_is_not_the_application_primary_key(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """Pi entry ID 只作溯源，不充当应用主键。"""
    kernel.entries = [
        {
            "id": "pi-entry-1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "你好"},
        },
        {
            "id": "pi-entry-2",
            "type": "message",
            "parentId": "pi-entry-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]},
        },
    ]
    await coordinator.send("你好")
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()

    mirrors = list(store.mirrors.values())
    assert {m.entry_id for m in mirrors} == {"pi-entry-1", "pi-entry-2"}
    # 镜像自己的主键与 Pi entry ID 不同
    assert all(m.id != m.entry_id for m in mirrors)
    # 能对上的镜像关联回应用消息；对不上的留空而不是硬塞
    user_msg = next(m for m in store.messages.values() if m.role is MessageRole.USER)
    linked = next(m for m in mirrors if m.entry_id == "pi-entry-1")
    assert linked.message_id == user_msg.id
    assert all(m.message_id != m.entry_id for m in mirrors)


async def test_mirror_does_not_duplicate_across_runs(
    coordinator: RunCoordinator, kernel: FakeKernel, store: InMemoryStore
) -> None:
    """同一 Pi 条目只镜像一次，不会每轮重复堆积。"""
    kernel.entries = [
        {
            "id": "e1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "一"},
        },
    ]
    for text in ("一", "二"):
        await coordinator.send(text)
        kernel.say("ACK")
        kernel.emit("run.settled", {})
        await coordinator.wait_idle()

    assert len([m for m in store.mirrors.values() if m.entry_id == "e1"]) == 1


# ---------------------------------------------------------------- 附加：事务语义


async def test_uow_rollback_discards_everything() -> None:
    """未提交的事务必须整体丢弃，不留半条记录。"""
    store = InMemoryStore()
    uow = in_memory_uow_factory(store)()
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Conversation

    uow.conversations.add(Conversation(id="c1", title="t", created_at=datetime.now(UTC)))
    assert uow.conversations.get("c1") is not None  # 事务内可见
    uow.rollback()

    assert store.conversations == {}
    assert uow.conversations.get("c1") is None


async def test_uow_commit_is_all_or_nothing() -> None:
    """提交后所有实体一起可见——这是"原子创建"的底层保证。"""
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Conversation
    from limbowave.domain.run import RunRecord

    store = InMemoryStore()
    uow = in_memory_uow_factory(store)()
    now = datetime.now(UTC)
    uow.conversations.add(Conversation(id="c1", title="t", created_at=now))
    uow.runs.add(
        RunRecord(
            id="r1",
            conversation_id="c1",
            branch_id="b1",
            status=RunStatus.RUNNING,
            created_at=now,
            user_message_id="m1",
        )
    )
    uow.commit()

    assert set(store.conversations) == {"c1"}
    assert set(store.runs) == {"r1"}


# ---------- resume：重启后续接既有会话 ----------


def _resume_context() -> RunContext:
    return RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat", "max_tokens": 4096},
    )


async def test_resume_attaches_to_existing_conversation(
    kernel: FakeKernel, store: InMemoryStore
) -> None:
    """send 过一轮后，新协调器 resume 同一会话，第二轮写进同一分支。"""
    _seed_one_turn(kernel)
    coord_a = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord_a.send("第一轮")
    assert coord_a.conversation_id is not None
    assert coord_a.branch_id is not None
    conv_id, branch_id = coord_a.conversation_id, coord_a.branch_id
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord_a.wait_idle()

    # 模拟重启：全新的协调器，同一个 backing store
    from limbowave.domain.runtime_state import RuntimeRestoreResult

    class RestoringKernel(FakeKernel):
        def capabilities(self) -> KernelCapabilities:
            return KernelCapabilities(frozenset({KernelCapability.RUNTIME_RESTORE}))

        async def restore_runtime_state(self, snapshot: Any) -> RuntimeRestoreResult:
            self.entries = list(snapshot.entries)
            return RuntimeRestoreResult(success=True)

    restored = RestoringKernel()
    coord_b = RunCoordinator(restored, in_memory_uow_factory(store), context=_resume_context)
    assert await coord_b.resume(conv_id, branch_id)
    assert coord_b.conversation_id == conv_id
    assert coord_b.branch_id == branch_id
    assert [e["id"] for e in restored.entries] == ["e1", "e2"]

    # resume 后的新一轮落在同一会话/分支，而不是另开新会话
    run_id = await coord_b.send("第二轮")
    assert run_id is not None
    with in_memory_uow_factory(store)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None
        assert run.conversation_id == conv_id
        assert run.branch_id == branch_id
        assert len(uow.conversations.list_all()) == 1


async def test_resume_rejects_unknown_ids(kernel: FakeKernel, store: InMemoryStore) -> None:
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    assert not await coord.resume("no-such-conv", "no-such-branch")
    assert coord.conversation_id is None  # 未污染会话定位


async def test_resume_rejects_mismatched_branch(kernel: FakeKernel, store: InMemoryStore) -> None:
    """分支必须属于该会话——错位引用拒绝。"""
    coord_a = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord_a.send("第一轮")
    assert coord_a.conversation_id and coord_a.branch_id

    coord_b = RunCoordinator(FakeKernel(), in_memory_uow_factory(store), context=_resume_context)
    assert not await coord_b.resume(coord_a.conversation_id, "other-branch")


async def test_resume_refused_while_busy(kernel: FakeKernel, store: InMemoryStore) -> None:
    """运行中拒绝切换会话定位。"""
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("进行中")
    assert coord.busy
    conv_id, branch_id = coord.conversation_id, coord.branch_id
    assert conv_id and branch_id
    assert await coord.resume(conv_id, branch_id) is False


# ---------- new_session：清空会话定位 ----------


async def test_new_session_clears_targeting(kernel: FakeKernel, store: InMemoryStore) -> None:
    """send 过一轮后 new_session，下一轮落在全新会话。"""
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("第一轮")
    first_conv = coord.conversation_id
    assert first_conv
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    assert await coord.new_session()
    assert coord.conversation_id is None
    assert coord.branch_id is None

    await coord.send("第二轮")
    assert coord.conversation_id != first_conv

    with in_memory_uow_factory(store)() as uow:
        assert len(uow.conversations.list_all()) == 2  # 旧会话仍在，未被清掉


async def test_new_session_refused_while_busy(kernel: FakeKernel, store: InMemoryStore) -> None:
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("进行中")
    assert coord.busy
    assert not await coord.new_session()
    assert coord.conversation_id is not None  # 定位未被清空


# ---------- 分支（Task 3.2）：编辑 / 重生成 / 切换 ----------


class BranchingKernel(FakeKernel):
    """支持分支的假内核：记录 fork 调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.forked: list[str] = []
        self.fork_error: Exception | None = None

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities(capabilities=frozenset({KernelCapability.BRANCHING}))

    async def fork(self, entry_id: str) -> str:
        if self.fork_error is not None:
            raise self.fork_error
        self.forked.append(entry_id)
        return ""


def _seed_one_turn(kernel: FakeKernel) -> None:
    """让内核 entries 含一对用户/助手消息，供镜像关联 message_id。"""
    kernel.entries = [
        {
            "id": "e1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "第一轮"},
        },
        {
            "id": "e2",
            "type": "message",
            "parentId": "e1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]},
        },
    ]


async def _one_turn(kernel: FakeKernel, coord: RunCoordinator) -> None:
    """跑完一整轮并落镜像。"""
    await coord.send("第一轮")
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


async def test_edit_user_message_forks_and_creates_branch(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    first_branch = coord.branch_id
    with in_memory_uow_factory(store)() as uow:
        msgs = uow.messages.list_for_branch(first_branch)
        user_msg = next(m for m in msgs if m.role is MessageRole.USER)

    # 编辑用户消息 → 新文本
    kernel.entries = []  # fork 后内核的新条目由后续轮次产生
    run_id = await coord.edit_user_message(user_msg.id, "第一轮（改）")

    assert run_id is not None
    assert kernel.forked == ["e1"]  # fork 在用户消息的 entry 上
    assert coord.branch_id != first_branch  # 定位到新分支

    with in_memory_uow_factory(store)() as uow:
        branches = uow.branches.list_for_conversation(coord.conversation_id)
        assert len(branches) == 2
        new_branch = next(b for b in branches if b.id == coord.branch_id)
        assert new_branch.parent_branch_id == first_branch
        assert new_branch.forked_from_message_id == user_msg.id

        # 原分支历史不动
        old_msgs = uow.messages.list_for_branch(first_branch)
        assert any(m.content == "第一轮" for m in old_msgs)
        # 新分支上是新文本
        new_msgs = uow.messages.list_for_branch(coord.branch_id)
        assert any(m.content == "第一轮（改）" for m in new_msgs)


async def test_edit_user_message_carries_attachments(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    with in_memory_uow_factory(store)() as uow:
        msgs = uow.messages.list_for_branch(coord.branch_id)
        user_msg = next(m for m in msgs if m.role is MessageRole.USER)

    kernel.entries = []
    run_id = await coord.edit_user_message(user_msg.id, "改", attachment_ids=["doc-1"])

    assert run_id is not None
    with in_memory_uow_factory(store)() as uow:
        intent = uow.snapshots.get_intent(run_id)
        assert intent is not None
        assert intent.attachment_ids == ("doc-1",)

# ---------- 文档注记与分叉：镜像匹配必须以实际 prompt 为基准 ----------

DOC_NOTE = "[用户附加了文档]\n- file_id: doc-1 · 名称: notes.md · 3 行"


def _document_turn_entries(
    prefix: str = "", parent_tail: str | None = None
) -> list[dict[str, Any]]:
    """一轮带注记的 Pi 条目：用户条目内容是**实际 prompt**（原文 + 注记）。"""
    return [
        {
            "id": f"{prefix}e1",
            "type": "message",
            "parentId": parent_tail,
            "message": {"role": "user", "content": f"第一轮\n\n{DOC_NOTE}"},
        },
        {
            "id": f"{prefix}e2",
            "type": "message",
            "parentId": f"{prefix}e1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]},
        },
    ]


def _seed_document_turn(
    kernel: FakeKernel, prefix: str = "", parent_tail: str | None = None
) -> None:
    """真实 Pi 会把**实际 prompt**（原文 + 注记）存进用户条目。"""
    kernel.entries = _document_turn_entries(prefix, parent_tail)


async def _document_turn(kernel: FakeKernel, coord: RunCoordinator) -> str | None:
    """跑完一整轮带文档注记的发送并落镜像。"""
    run_id = await coord.send("第一轮", attachment_ids=["doc-1"], document_note=DOC_NOTE)
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    return run_id


def _user_message(store: InMemoryStore, branch_id: str, content: str) -> Any:
    with in_memory_uow_factory(store)() as uow:
        msgs = uow.messages.list_for_branch(branch_id)
        return next(m for m in msgs if m.role is MessageRole.USER and m.content == content)


def _mirror_for(store: InMemoryStore, conversation_id: str, entry_id: str) -> Any:
    with in_memory_uow_factory(store)() as uow:
        return next(
            m
            for m in uow.runtime.list_for_conversation(conversation_id)
            if m.entry_id == entry_id
        )


async def test_edit_message_with_document_note_forks(store: InMemoryStore) -> None:
    """带注记的消息：镜像以实际 prompt 关联，编辑能正常分叉（回归：曾永远落空）。"""
    kernel = BranchingKernel()
    _seed_document_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    assert await _document_turn(kernel, coord) is not None
    user_msg = _user_message(store, coord.branch_id, "第一轮")
    # 直接关联成功：不走补链
    assert _mirror_for(store, coord.conversation_id, "e1").message_id == user_msg.id

    kernel.entries = []
    run_id = await coord.edit_user_message(user_msg.id, "第一轮（改）")

    assert run_id is not None
    assert kernel.forked == ["e1"]
    assert coord.branch_id != user_msg.branch_id


async def test_edit_relinks_orphan_mirror_left_by_note_bug(store: InMemoryStore) -> None:
    """存量修复：旧版按原文匹配落空的孤儿镜像，拼回实际 prompt 唯一命中时补链。"""
    kernel = BranchingKernel()
    _seed_document_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    coord.attachment_builder = lambda ids: AttachmentPayload(
        attachment_ids=list(ids), document_note=DOC_NOTE
    )
    assert await _document_turn(kernel, coord) is not None
    user_msg = _user_message(store, coord.branch_id, "第一轮")
    # 回放旧版现场：镜像 message_id 悬空（直接越过仓库——补链只允许空→非空）
    broken = _mirror_for(store, coord.conversation_id, "e1")
    store.mirrors[broken.id] = replace(broken, message_id=None)

    kernel.entries = []
    run_id = await coord.edit_user_message(user_msg.id, "第一轮（改）")

    assert run_id is not None
    assert kernel.forked == ["e1"]
    assert _mirror_for(store, coord.conversation_id, "e1").message_id == user_msg.id


async def test_relink_refused_when_orphans_ambiguous(store: InMemoryStore) -> None:
    """两条同文同注记的消息留下两个孤儿镜像：宁可拒绝也不把分叉锚到猜测的 entry。"""
    kernel = BranchingKernel()
    _seed_document_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    coord.attachment_builder = lambda ids: AttachmentPayload(
        attachment_ids=list(ids), document_note=DOC_NOTE
    )
    assert await _document_turn(kernel, coord) is not None
    # 第二轮同文同注记：Pi 条目接在第一轮之后（父链与真实续聊一致）
    kernel.entries = [
        *_document_turn_entries(),
        *_document_turn_entries(prefix="t2_", parent_tail="e2"),
    ]
    assert await _document_turn(kernel, coord) is not None
    user_msg = _user_message(store, coord.branch_id, "第一轮")
    for orphan_id in ("e1", "t2_e1"):
        broken = _mirror_for(store, coord.conversation_id, orphan_id)
        store.mirrors[broken.id] = replace(broken, message_id=None)

    kernel.entries = []
    assert await coord.edit_user_message(user_msg.id, "第一轮（改）") is None
    assert kernel.forked == []

async def test_edit_rejects_non_user_message(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    with in_memory_uow_factory(store)() as uow:
        msgs = uow.messages.list_for_branch(coord.branch_id)
        assistant_msg = next(m for m in msgs if m.role is MessageRole.ASSISTANT)

    assert await coord.edit_user_message(assistant_msg.id, "x") is None
    assert kernel.forked == []  # 没有分叉


async def test_edit_requires_branching_capability(store: InMemoryStore) -> None:
    """内核不支持 BRANCHING 时拒绝，不静默。"""
    kernel = FakeKernel()  # 无能力
    kernel.entries = [
        {
            "id": "e1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "第一轮"},
        },
    ]
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("第一轮")
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with in_memory_uow_factory(store)() as uow:
        user_msg = next(
            m for m in uow.messages.list_for_branch(coord.branch_id) if m.role is MessageRole.USER
        )
    assert await coord.edit_user_message(user_msg.id, "改") is None


async def test_regenerate_forks_at_prior_user_message(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    first_branch = coord.branch_id
    with in_memory_uow_factory(store)() as uow:
        msgs = uow.messages.list_for_branch(first_branch)
        assistant_msg = next(m for m in msgs if m.role is MessageRole.ASSISTANT)

    kernel.entries = []
    run_id = await coord.regenerate(assistant_msg.id)

    assert run_id is not None
    assert kernel.forked == ["e1"]  # 回溯到用户消息 entry
    # 重发的是原用户文本
    assert kernel.sent[-1] == "第一轮"
    assert coord.branch_id != first_branch


async def test_regenerate_rejects_user_message(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    with in_memory_uow_factory(store)() as uow:
        user_msg = next(
            m for m in uow.messages.list_for_branch(coord.branch_id) if m.role is MessageRole.USER
        )
    assert await coord.regenerate(user_msg.id) is None
    assert kernel.forked == []


async def test_branch_refused_while_busy(store: InMemoryStore) -> None:
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    with in_memory_uow_factory(store)() as uow:
        user_msg = next(
            m for m in uow.messages.list_for_branch(coord.branch_id) if m.role is MessageRole.USER
        )
    # 让协调器处于 busy
    await coord.send("进行中")
    assert coord.busy
    assert await coord.edit_user_message(user_msg.id, "x") is None
    assert kernel.forked == []


async def test_switch_branch_requires_restore_capability(store: InMemoryStore) -> None:
    """内核不支持 RUNTIME_RESTORE 时拒绝切换（不静默聊串台）。"""
    kernel = BranchingKernel()  # 只有 BRANCHING，没有 RUNTIME_RESTORE
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _one_turn(kernel, coord)
    first_branch = coord.branch_id
    with in_memory_uow_factory(store)() as uow:
        user_msg = next(
            m for m in uow.messages.list_for_branch(first_branch) if m.role is MessageRole.USER
        )
    # 编辑产生第二分支
    kernel.entries = []
    await coord.edit_user_message(user_msg.id, "第二轮")
    second_branch = coord.branch_id
    assert second_branch != first_branch

    # 切回第一分支：内核不支持恢复 → 拒绝
    assert await coord.switch_branch(first_branch) is False
    assert coord.branch_id == second_branch  # 定位没变


# ---------- 工具步骤配对（Phase 9 / §三.2） ----------


async def test_tool_steps_paired_by_call_id(kernel: FakeKernel, store: InMemoryStore) -> None:
    """start/end 按 toolCallId 配对，落成助手消息上的工具步骤。"""
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("跑个工具")

    kernel.emit(
        "tool.start", {"toolCallId": "call-1", "toolName": "read", "args": {"path": "a.txt"}}
    )
    kernel.emit(
        "tool.start", {"toolCallId": "call-2", "toolName": "bash", "args": {"command": "ls"}}
    )
    kernel.emit(
        "tool.end",
        {
            "toolCallId": "call-2",
            "toolName": "bash",
            "result": {"output": "file1\nfile2"},
            "isError": False,
        },
    )
    kernel.emit(
        "tool.end",
        {
            "toolCallId": "call-1",
            "toolName": "read",
            "result": {"error": "文件不存在"},
            "isError": True,
        },
    )
    kernel.say("完成")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    with in_memory_uow_factory(store)() as uow:
        messages = uow.messages.list_for_branch(coord.branch_id)
    assistant = next(m for m in messages if m.role is MessageRole.ASSISTANT)
    steps = {s.tool_call_id: s for s in assistant.tool_steps}
    assert set(steps) == {"call-1", "call-2"}
    assert steps["call-1"].name == "read"
    assert steps["call-1"].status is ToolStatus.ERROR
    assert "文件不存在" in (steps["call-1"].error or "")
    assert steps["call-2"].status is ToolStatus.OK
    assert "file1" in steps["call-2"].result_summary
    # 参数被记下来（展开态要显示）
    assert steps["call-1"].args == {"path": "a.txt"}


async def test_orphan_tool_end_still_recorded(kernel: FakeKernel, store: InMemoryStore) -> None:
    """只有结束事件（迟到的 start）：仍然留审计，不丢记录。"""
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("x")
    kernel.emit(
        "tool.end", {"toolCallId": "orphan", "toolName": "write", "result": "ok", "isError": False}
    )
    kernel.say("done")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    with in_memory_uow_factory(store)() as uow:
        assistant = next(
            m
            for m in uow.messages.list_for_branch(coord.branch_id)
            if m.role is MessageRole.ASSISTANT
        )
    assert [s.tool_call_id for s in assistant.tool_steps] == ["orphan"]
    assert assistant.tool_steps[0].status is ToolStatus.OK


async def test_tool_steps_absent_without_tools(kernel: FakeKernel, store: InMemoryStore) -> None:
    """没有工具调用时不留空记录。"""
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await coord.send("普通对话")
    kernel.say("回答")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    with in_memory_uow_factory(store)() as uow:
        assistant = next(
            m
            for m in uow.messages.list_for_branch(coord.branch_id)
            if m.role is MessageRole.ASSISTANT
        )
    assert assistant.tool_steps == ()


# ---------- 自动重试（§八.3） ----------


def _retry_context(*, max_attempts: int = 3, base_delay_ms: int = 0) -> RunContext:
    from limbowave.domain.retry import RetryPolicy

    return RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat"},
        retry_policy=RetryPolicy(
            max_attempts=max_attempts, base_delay_ms=base_delay_ms, multiplier=1.0
        ),
    )


async def test_retryable_failure_is_retried(store: InMemoryStore) -> None:
    """连接类错误自动重试：同一个 run 内重发，界面收到 retrying 事件。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _retry_context())
    events: list[tuple[str, dict]] = []
    coord.subscribe(lambda e: events.append((e.kind, e.data)))

    run_id = await coord.send("重试测试")
    assert run_id is not None
    # 第一次尝试失败（连接类）
    kernel.emit(
        "message.end",
        {"message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET"}},
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    assert kernel.sent == ["重试测试", "重试测试"]  # 重发了一次
    retrying = [d for k, d in events if k == "retrying"]
    assert len(retrying) == 1
    assert "连接失败" in retrying[0]["reason"]
    assert retrying[0]["max_attempts"] == 3


async def test_auth_failure_not_retried(store: InMemoryStore) -> None:
    """鉴权失败不重试（计划书明确：重试也不会变好）。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _retry_context())
    run_id = await coord.send("鉴权会失败")
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "invalid api key",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    assert kernel.sent == ["鉴权会失败"]  # 只发了一次
    with in_memory_uow_factory(store)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None and run.status is RunStatus.FAILED


async def test_retry_gives_up_after_max_attempts(store: InMemoryStore) -> None:
    """次数用尽后落 failed，不再无限重试。"""
    kernel = FakeKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _retry_context(max_attempts=2)
    )
    run_id = await coord.send("一直失败")

    for _ in range(3):  # 无论失败几次都只该重发一次（max_attempts=2）
        kernel.emit(
            "message.end",
            {
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "ECONNREFUSED",
                }
            },
        )
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        if not coord.busy:
            break

    assert kernel.sent == ["一直失败", "一直失败"]  # 首次 + 一次重试
    with in_memory_uow_factory(store)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None and run.status is RunStatus.FAILED


async def test_tool_side_effect_never_retried(store: InMemoryStore) -> None:
    """已跑过工具：即使错误是连接类也不重试（否则副作用会重复）。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _retry_context())
    run_id = await coord.send("会跑工具")
    kernel.emit("tool.start", {"toolCallId": "c1", "toolName": "write", "args": {"path": "x"}})
    kernel.emit(
        "tool.end", {"toolCallId": "c1", "toolName": "write", "result": "ok", "isError": False}
    )
    kernel.emit(
        "message.end",
        {"message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET"}},
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    assert kernel.sent == ["会跑工具"]  # 没有重发
    with in_memory_uow_factory(store)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None and run.status is RunStatus.FAILED


async def test_retry_records_attempts_in_transports(store: InMemoryStore) -> None:
    """重试记录进传输快照（§十三.1）：失败尝试与成功尝试都留档，带 attempt 号。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _retry_context())
    run_id = await coord.send("记录重试")

    # 第一次尝试：有请求观测 + 连接失败
    kernel.observe({"kind": "provider.request", "payload": {"model": "deepseek-chat"}})
    kernel.emit(
        "message.end",
        {"message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET"}},
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    # 第二次尝试：成功
    kernel.observe({"kind": "provider.request", "payload": {"model": "deepseek-chat"}})
    kernel.say("成功了")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    with in_memory_uow_factory(store)() as uow:
        transports = uow.snapshots.list_transport(run_id)
    attempts = [t.attempt for t in transports]
    assert attempts == [1, 2], f"应保留两次尝试的记录，实得 {attempts}"
    with in_memory_uow_factory(store)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None and run.status is RunStatus.COMPLETED


async def test_no_retry_when_policy_disabled(store: InMemoryStore) -> None:
    """站点预设 max_attempts=1 → 不重试（策略来自站点配置，不是全局写死）。"""
    kernel = FakeKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _retry_context(max_attempts=1)
    )
    await coord.send("不重试")
    kernel.emit(
        "message.end",
        {"message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET"}},
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    assert kernel.sent == ["不重试"]
