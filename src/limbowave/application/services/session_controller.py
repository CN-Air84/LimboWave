"""会话控制器：面向 GUI 的薄 facade。

Phase 1B 起，业务生命周期（run 记录、消息落库、双快照、Runtime 镜像）全部由
:class:`RunCoordinator` 承担；本类只负责：

- 把 GUI 命令转给协调器；
- 保持与 GUI 约定的展示事件词汇（``ChatEvent``）；
- 权限裁决的中转（GUI 弹窗属于展示层，不属于运行编排）；
- **切换会话**：协调器定位 + 内核侧上下文切换（``switch_session``）的组合动作。

**它不持有权威业务状态**：会话、消息、运行都在仓库里，这里只转发。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from limbowave.application.events import ChatEvent, ChatEventHandler
from limbowave.application.kernel import (
    AgentKernel,
    KernelState,
    PermissionHandler,
)
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.memory_service import MemoryService
from limbowave.application.services.run_coordinator import (
    RunContext,
    RunCoordinator,
)
from limbowave.domain.memory import MemoryRunContext
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

__all__ = ["ChatEvent", "ChatEventHandler", "SessionController"]

_UNROUTED = RunContext(
    logical_model_id="unknown",
    endpoint_id="unknown",
    routing_reason="未装配路由上下文（仅用于展示事件与临时落库）",
)


class SessionController:
    """GUI 与 :class:`RunCoordinator` 之间的薄 facade。"""

    def __init__(
        self,
        kernel: AgentKernel | None,
        coordinator: RunCoordinator | None = None,
        *,
        uow_factory: UnitOfWorkFactory | None = None,
        context: Callable[[], RunContext] | None = None,
    ) -> None:
        factory = uow_factory or in_memory_uow_factory()
        self._coordinator = coordinator or RunCoordinator(
            kernel,
            factory,
            context=context or (lambda: _UNROUTED),
        )

    def set_memory_service(self, service: MemoryService) -> None:
        self._coordinator.memory_service = service

    @property
    def memory_context(self) -> MemoryRunContext | None:
        return self._coordinator.memory_context

    @property
    def branch_id(self) -> str | None:
        return self._coordinator.branch_id

    # ---------- 状态 ----------

    @property
    def available(self) -> bool:
        """内核是否就绪（未就绪时 GUI 应禁用输入并提示配置）。"""
        return self._coordinator.available

    @property
    def busy(self) -> bool:
        return self._coordinator.busy

    @property
    def run_id(self) -> str | None:
        return self._coordinator.run_id

    @property
    def conversation_id(self) -> str | None:
        return self._coordinator.conversation_id

    # ---------- 订阅与权限 ----------

    def subscribe(self, handler: ChatEventHandler) -> Callable[[], None]:
        return self._coordinator.subscribe(handler)

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._coordinator.set_permission_handler(handler)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self._coordinator.start()

    async def shutdown(self) -> None:
        await self._coordinator.shutdown()

    async def wait_idle(self, timeout: float = 15.0) -> None:
        """等待待落的收尾事务。应用退出前应调用。"""
        await self._coordinator.wait_idle(timeout)

    async def state(self) -> KernelState | None:
        return await self._coordinator.state()

    # ---------- 命令 ----------

    async def send(
        self,
        text: str,
        *,
        attachment_ids: list[str] | None = None,
        images: list[dict[str, Any]] | None = None,
        document_note: str | None = None,
    ) -> str | None:
        return await self._coordinator.send(
            text,
            attachment_ids=attachment_ids,
            images=images,
            document_note=document_note,
        )

    async def abort(self) -> None:
        await self._coordinator.abort()

    async def mark_interrupted(self, reason: str = "Runtime 异常退出") -> None:
        await self._coordinator.mark_interrupted(reason)

    async def resume(self, conversation_id: str, branch_id: str) -> bool:
        """续接既有会话（应用重启后）。下一轮 send 写入该分支而非另开新会话。"""
        return await self._coordinator.switch_conversation(conversation_id, branch_id)

    async def new_session(self) -> bool:
        """新建空内核会话，成功后清空应用定位。运行中拒绝。"""
        return await self._coordinator.new_session()

    # ---------- 分支（Task 3.2） ----------

    async def edit_user_message(
        self,
        message_id: str,
        new_text: str,
        *,
        attachment_ids: list[str] | None = None,
        images: list[dict[str, Any]] | None = None,
        document_note: str | None = None,
    ) -> str | None:
        """编辑用户消息：自动分叉新分支并发送新文本（连同编辑后的附件）。原分支历史不动。"""
        return await self._coordinator.edit_user_message(
            message_id,
            new_text,
            attachment_ids=attachment_ids,
            images=images,
            document_note=document_note,
        )

    async def retry_user_message(self, message_id: str) -> str | None:
        """重试失败或停止的用户请求：在当前分支重发原文，不创建分支。"""
        return await self._coordinator.retry_user_message(message_id)

    async def regenerate(self, assistant_message_id: str) -> str | None:
        """完整回复分叉重生成；失败或停止的回复在当前分支重试。"""
        return await self._coordinator.regenerate(assistant_message_id)

    async def switch_branch(
        self, branch_id: str, *, known_has_messages: bool | None = None
    ) -> bool:
        """切换到同会话的另一条分支（应用定位 + 内核恢复）。"""
        conversation_id = self._coordinator.conversation_id
        if conversation_id is None:
            return False
        return await self._coordinator.switch_conversation(
            conversation_id,
            branch_id,
            known_has_messages=known_has_messages,
        )

    async def switch_conversation(
        self,
        conversation_id: str,
        branch_id: str,
        *,
        known_has_messages: bool | None = None,
    ) -> bool:
        """恢复目标分支上下文后才更新应用定位。"""
        return await self._coordinator.switch_conversation(
            conversation_id,
            branch_id,
            known_has_messages=known_has_messages,
        )

    # ---------- 逃生舱 ----------

    def coordinator(self) -> RunCoordinator:
        """暴露协调器供上层查询（运行记录、双快照、Runtime 镜像等）。"""
        return self._coordinator
