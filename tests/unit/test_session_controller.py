"""SessionController 的单元测试：用一个 FakeKernel 驱动，不碰真实进程。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from limbowave.application.kernel import (
    AgentKernel,
    CompactionResult,
    EventHandler,
    KernelCapabilities,
    KernelEvent,
    KernelState,
    PermissionHandler,
)
from limbowave.application.services.session_controller import SessionController


class FakeKernel(AgentKernel):
    """内存假内核：记录命令，允许测试直接注入事件。"""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.sent: list[str] = []
        self.aborted = False
        self.permission_handler: PermissionHandler | None = None

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities()

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        self.sent.append(text)

    async def new_session(self) -> None:
        pass

    async def abort(self) -> None:
        self.aborted = True

    async def get_state(self) -> KernelState:
        return KernelState(
            model_id="fake",
            thinking_level="off",
            is_streaming=False,
            is_compacting=False,
            session_id="fake-session",
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

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self.permission_handler = handler

    # 测试助手：注入一个内核事件
    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            handler(KernelEvent(kind=kind, payload=payload))


def _collect(controller: SessionController) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    controller.subscribe(lambda e: events.append((e.kind, e.data)))
    return events


def _assistant_message(text: str, stop: str = "stop") -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": stop}


async def test_send_delegates_to_kernel() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    await controller.send("  你好  ")

    assert kernel.sent == ["你好"]
    # user 事件带 message_id（分支操作以消息为锚点）
    user_events = [d for k, d in events if k == "user"]
    assert len(user_events) == 1
    assert user_events[0]["text"] == "你好"
    assert user_events[0]["message_id"]
    assert controller.busy


async def test_streaming_accumulates_and_finalizes() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    await controller.send("hi")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit("message.update", {"assistantMessageEvent": {"type": "text_delta", "delta": "你"}})
    kernel.emit("message.update", {"assistantMessageEvent": {"type": "text_delta", "delta": "好"}})
    kernel.emit("message.end", {"message": _assistant_message("你好")})
    kernel.emit("run.settled", {})

    kinds = [k for k, _ in events]
    assert "assistant_start" in kinds
    deltas = [d["text"] for k, d in events if k == "assistant_delta"]
    assert deltas == ["你", "好"]
    end = next(d for k, d in events if k == "assistant_end")
    assert end["text"] == "你好"
    assert not controller.busy  # settled 后置为不忙
    assert ("settled", {}) in events


async def test_thinking_delta_is_separate_channel() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    await controller.send("hi")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit(
        "message.update", {"assistantMessageEvent": {"type": "thinking_delta", "delta": "想想"}}
    )

    assert any(k == "thinking_delta" and d["text"] == "想想" for k, d in events)
    assert not any(k == "assistant_delta" for k, d in events)


async def test_error_stop_reason_surfaces() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    await controller.send("hi")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit(
        "message.end", {"message": {**_assistant_message("", stop="error"), "errorMessage": "boom"}}
    )

    assert any(k == "error" and d["message"] == "boom" for k, d in events)


async def test_busy_guard_rejects_second_send() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    await controller.send("first")
    await controller.send("second")

    assert kernel.sent == ["first"]
    assert any(k == "error" for k, _ in events)


async def test_abort_delegates() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    await controller.abort()
    assert kernel.aborted


async def test_tool_activity_events() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)
    events = _collect(controller)

    kernel.emit("tool.start", {"toolName": "read"})
    kernel.emit("tool.end", {"toolName": "read", "isError": False})

    tool_events = [d for k, d in events if k == "tool"]
    assert tool_events[0]["phase"] == "start"
    assert tool_events[1]["phase"] == "end"


async def test_no_kernel_is_unavailable_and_send_errors() -> None:
    controller = SessionController(None)
    events = _collect(controller)

    assert not controller.available
    await controller.send("hi")

    assert any(k == "error" and "未配置" in d["message"] for k, d in events)


async def test_permission_defaults_to_deny_without_handler() -> None:
    kernel = FakeKernel()
    SessionController(kernel)
    # 控制器已把自己的 _on_permission 注册给内核；无应用 handler 时应拒绝
    assert kernel.permission_handler is not None
    assert await kernel.permission_handler("允许？", "rm -rf /") is False


async def test_permission_routes_to_app_handler() -> None:
    kernel = FakeKernel()
    controller = SessionController(kernel)

    async def _allow(title: str, detail: str) -> bool:
        return True

    controller.set_permission_handler(_allow)
    assert kernel.permission_handler is not None
    assert await kernel.permission_handler("允许？", "read x") is True


async def test_switch_conversation_without_restore_capability() -> None:
    """不能恢复历史时必须拒绝，不能仅改定位后沿用旧上下文。"""

    kernel = FakeKernel()  # 默认 capabilities 为空
    controller = SessionController(kernel)

    # 先聊一轮落库，再开新会话，再切回去
    await controller.send("第一轮")
    conv_id = controller.conversation_id
    assert conv_id
    kernel.emit("assistant_delta", {"text": "ACK"})
    kernel.emit("run.settled", {})
    await controller.wait_idle()

    coordinator = controller.coordinator()
    branch_id = coordinator.branch_id
    assert branch_id
    assert await controller.new_session()

    assert not await controller.switch_conversation(conv_id, branch_id)
    assert controller.conversation_id is None


async def test_switch_conversation_rejects_unknown() -> None:
    controller = SessionController(FakeKernel())
    assert not await controller.switch_conversation("nope", "nope")
