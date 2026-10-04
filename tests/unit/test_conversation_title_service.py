"""首轮会话标题生成：隔离调用、清洗与失败降级。"""

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
from limbowave.application.services.conversation_title_service import (
    ConversationTitleService,
    normalize_title,
)


class _TitleKernel(AgentKernel):
    def __init__(self, response: str = "模型生成的标题") -> None:
        self.response = response
        self.handlers: list[EventHandler] = []
        self.started = False
        self.stopped = False
        self.sent = ""
        self.model: tuple[str, str] | None = None
        self.thinking_level: str | None = None

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities()

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True

    async def send_message(
        self, text: str, *, images: list[dict[str, Any]] | None = None
    ) -> None:
        self.sent = text
        self._emit(
            "message.end",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": self.response}],
                }
            },
        )
        self._emit("run.settled", {})

    async def abort(self) -> None: ...

    async def get_state(self) -> KernelState:
        return KernelState("fake", "off", False, False, "s", None, 0, 0)

    async def set_model(self, provider: str, model_id: str) -> None:
        self.model = (provider, model_id)

    async def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        return []

    async def fork(self, entry_id: str) -> str:
        return ""

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult:
        return CompactionResult("", 0)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self.handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self.handlers:
                self.handlers.remove(handler)

        return _unsubscribe

    async def events(self) -> AsyncIterator[KernelEvent]:
        return
        yield  # pragma: no cover

    def set_permission_handler(self, handler: PermissionHandler | None) -> None: ...

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        for handler in list(self.handlers):
            handler(KernelEvent(kind, payload))


class _OwnerKernel(_TitleKernel):
    def __init__(self, isolated: AgentKernel | None) -> None:
        super().__init__()
        self.isolated = isolated

    def create_isolated(self) -> AgentKernel | None:
        return self.isolated


async def test_suggest_uses_isolated_kernel_without_touching_owner_context() -> None:
    isolated = _TitleKernel('标题："部署流水线。"')
    owner = _OwnerKernel(isolated)

    result = await ConversationTitleService().suggest(
        owner,
        "怎么部署？",
        "先配置 CI。",
        provider="relay-a",
        model_id="deepseek-chat",
    )

    assert result == "部署流水线"
    assert owner.sent == ""
    assert isolated.started and isolated.stopped
    assert isolated.model == ("relay-a", "deepseek-chat")
    assert isolated.thinking_level == "off"
    assert "怎么部署？" in isolated.sent
    assert "先配置 CI。" in isolated.sent
    assert not isolated.handlers


async def test_suggest_gracefully_skips_when_kernel_cannot_be_isolated() -> None:
    owner = _OwnerKernel(None)
    assert (
        await ConversationTitleService().suggest(
            owner, "用户", "助手", provider="p", model_id="m"
        )
        is None
    )


def test_normalize_title_rejects_empty_and_limits_length() -> None:
    assert normalize_title("\n\n") is None
    assert normalize_title("会话标题：《测试标题！》") == "测试标题"
    assert normalize_title("甲" * 60) == "甲" * 40
