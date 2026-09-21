"""会话控制器：把 AgentKernel 翻译成聊天级事件，供 GUI 消费。

架构约束：
- 本类在**应用层**，不 import Qt。GUI 经回调与它交互。
- 它不持有权威业务状态（会话树、消息历史在领域层 / Pi 会话树里）；
  这里只维护**当前轮的流式展示态**（assistant 增量累积），属瞬态。
- 权限裁决经 ``set_permission_handler`` 中转给上层（GUI 弹窗），默认拒绝。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from limbowave.application.kernel import AgentKernel, KernelEvent, KernelState, PermissionHandler


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """控制器发出的聊天级事件。"""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


ChatEventHandler = Callable[[ChatEvent], None]


class SessionController:
    """驱动一个 AgentKernel，把协议事件归一为聊天级事件。"""

    def __init__(self, kernel: AgentKernel | None) -> None:
        self._kernel = kernel
        self._handlers: list[ChatEventHandler] = []
        self._permission_handler: PermissionHandler | None = None
        self._busy = False
        self._current_text = ""
        self._current_thinking = ""
        if kernel is not None:
            kernel.subscribe(self._on_kernel_event)
            kernel.set_permission_handler(self._on_permission)

    # ---------- 可用性 ----------

    @property
    def available(self) -> bool:
        """内核是否就绪（未就绪时 GUI 应禁用输入并提示配置）。"""
        return self._kernel is not None

    @property
    def busy(self) -> bool:
        return self._busy

    # ---------- 订阅 ----------

    def subscribe(self, handler: ChatEventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._permission_handler = handler

    def _emit(self, kind: str, **data: Any) -> None:
        event = ChatEvent(kind=kind, data=data)
        for handler in list(self._handlers):
            # 订阅者异常不得拖垮控制器
            with contextlib.suppress(Exception):
                handler(event)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._kernel is not None:
            await self._kernel.start()

    async def shutdown(self) -> None:
        if self._kernel is not None:
            await self._kernel.shutdown()

    async def state(self) -> KernelState | None:
        if self._kernel is None:
            return None
        return await self._kernel.get_state()

    # ---------- 命令 ----------

    async def send(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._kernel is None:
            self._emit("error", message="未配置可用的模型内核")
            return
        if self._busy:
            self._emit("error", message="上一条回复还在进行中，请先等待或停止")
            return
        self._busy = True
        self._emit("user", text=text)
        try:
            await self._kernel.send_message(text)
        except Exception as exc:  # 发送本身失败（进程死了等）
            self._busy = False
            self._emit("error", message=f"发送失败：{exc}")
            self._emit("settled")

    async def abort(self) -> None:
        if self._kernel is not None:
            await self._kernel.abort()

    # ---------- 权限 ----------

    async def _on_permission(self, title: str, detail: str) -> bool:
        if self._permission_handler is None:
            return False
        try:
            return bool(await self._permission_handler(title, detail))
        except Exception:
            return False

    # ---------- 内核事件翻译 ----------

    def _on_kernel_event(self, event: KernelEvent) -> None:
        kind = event.kind
        payload = event.payload

        if kind == "message.start":
            role = (payload.get("message") or {}).get("role")
            if role == "assistant":
                self._current_text = ""
                self._current_thinking = ""
                self._emit("assistant_start")

        elif kind == "message.update":
            sub = payload.get("assistantMessageEvent") or {}
            sub_type = sub.get("type")
            if sub_type == "text_delta":
                delta = str(sub.get("delta", ""))
                self._current_text += delta
                self._emit("assistant_delta", text=delta)
            elif sub_type == "thinking_delta":
                delta = str(sub.get("delta", ""))
                self._current_thinking += delta
                self._emit("thinking_delta", text=delta)

        elif kind == "message.end":
            message = payload.get("message") or {}
            if message.get("role") == "assistant":
                text = self._extract_text(message)
                stop = message.get("stopReason")
                self._emit(
                    "assistant_end",
                    text=text,
                    stop_reason=stop,
                    thinking=self._current_thinking,
                )
                if stop == "error":
                    self._emit("error", message=str(message.get("errorMessage", "模型请求失败")))

        elif kind == "tool.start":
            self._emit("tool", name=payload.get("toolName"), phase="start")
        elif kind == "tool.end":
            self._emit(
                "tool",
                name=payload.get("toolName"),
                phase="end",
                is_error=bool(payload.get("isError", False)),
            )

        elif kind == "run.settled":
            self._busy = False
            self._emit("settled")

        elif kind == "extension.error":
            self._emit("error", message=str(payload.get("error", "扩展错误")))

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        """从 AssistantMessage 的 content 块中取文本。"""
        parts: list[str] = []
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
