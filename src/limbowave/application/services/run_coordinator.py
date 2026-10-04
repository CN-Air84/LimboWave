"""运行编排：一轮请求的完整生命周期与事务边界。

职责划分（Phase 1B 验收）：

- ``RunCoordinator``：一轮的生命周期与事务编排（本文件）。
- ``SessionController``：GUI 命令与展示事件（薄 facade）。
- ``AgentKernel``：执行与运行时事件。
- ``UnitOfWork`` / Repositories：应用权威数据。

一轮的标准顺序（固定，不随实现漂移）：

    创建 RunRecord → 保存用户消息 → 创建 RequestIntentSnapshot
    → 调用 AgentKernel → 转发流式展示事件
    → 收集 provider.request / provider.response
    → 保存最终助手消息 → 获取 Pi entries → 保存 RuntimeEntryMirror
    → 标记 RunRecord 最终状态

两条硬性约定：

1. **不静默删除用户输入**：用户消息、RunRecord 与意图快照在调用内核**之前**就原子提交，
   因此内核启动失败也留有完整记录。
2. **落盘在 settled 之后异步完成**，``wait_idle()`` 是收敛屏障——GUI 不会被落盘阻塞，
   但应用退出前必须 await 它。这是有意取舍：展示的实时性优先于落盘的同步性。

本阶段不做 Runtime 重建（Phase 1C），只保证恢复所需的数据被正确、完整地存下来。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from limbowave.application import branch_path
from limbowave.application.events import (
    ASSISTANT_DELTA,
    ASSISTANT_END,
    ASSISTANT_START,
    BRANCHED,
    ERROR,
    FIRST_RESPONSE_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    SETTLED,
    THINKING_DELTA,
    TOOL,
    USER,
    ChatEvent,
    ChatEventHandler,
)
from limbowave.application.kernel import (
    AgentKernel,
    KernelCapability,
    KernelEvent,
    KernelState,
    PermissionHandler,
)
from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.attachment_service import AttachmentPayload
from limbowave.application.services.memory_service import MemoryService, capture_memory, fork_memory
from limbowave.application.services.runtime_state_service import (
    RuntimeStateService,
    isolate_conversation_entries,
)
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole, MessageStatus
from limbowave.domain.memory import MemoryRunContext
from limbowave.domain.redaction import redact_body, redact_headers, redact_text
from limbowave.domain.retry import (
    ErrorClass,
    RetryAttempt,
    RetryPolicy,
    classify,
)
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.domain.snapshots import (
    RequestIntentSnapshot,
    TransportSnapshot,
    diff_params,
)
from limbowave.domain.stream_tape import StreamTape
from limbowave.domain.tool_step import ToolStep
from limbowave.domain.tool_step import finish as finish_tool_step

DEFAULT_CONVERSATION_TITLE = "新会话"
_LOG = logging.getLogger(__name__)
_DIAGNOSTIC_EVENTS = {
    USER: (logging.INFO, "run.started"),
    ERROR: (logging.ERROR, "run.error"),
    RUN_FAILED: (logging.ERROR, "run.failed"),
    RUN_INTERRUPTED: (logging.WARNING, "run.interrupted"),
    BRANCHED: (logging.INFO, "conversation.branched"),
    "retrying": (logging.WARNING, "run.retrying"),
    TOOL: (logging.INFO, "run.tool"),
}


@dataclass(frozen=True, slots=True)
class RunContext:
    """本轮的路由上下文，用于请求意图快照。"""

    logical_model_id: str
    endpoint_id: str
    routing_reason: str
    app_params: dict[str, Any] = field(default_factory=dict)
    # 当前逻辑模型的视觉能力（Task 4.4：图片发送前检查，不支持则不静默 OCR）
    supports_images: bool = False
    # 站点的重试策略（§八.3）。None = 用默认策略
    retry_policy: RetryPolicy | None = None
    thinking_level: str | None = None


ContextProvider = Callable[[], RunContext]


@dataclass(frozen=True, slots=True)
class _SwitchPreparation:
    valid: bool
    has_messages: bool = False
    snapshot: Any | None = None


@dataclass(slots=True)
class _LiveRun:
    """一轮运行的内存态。**不持久化**，只在 run 进行期间存在。"""

    run_id: str
    conversation_id: str
    branch_id: str
    user_message_id: str
    assistant_message_id: str
    user_text: str
    context: RunContext
    # 实际发给内核的 prompt（含文档注记）；镜像匹配以它为用户条目的基准
    sent_prompt: str | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    route_error: bool = False
    first_turn: bool = False
    text: str = ""
    thinking: str = ""
    stop_reason: str | None = None
    error: str | None = None
    # provider 观测（按发生顺序）
    requests: list[dict[str, Any]] = field(default_factory=list)
    request_headers: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    # §十三.1「流式事件」与「原始响应」：按**第几次 provider 请求**归集（从 0 起）。
    # 用请求序号而不是助手消息序号做键：一次 run 里重试与工具往返都会新增请求，
    # 请求序号是唯一能把「这段流是谁吐的」说清楚的锚点。
    active_request_index: int = -1
    tapes: dict[int, StreamTape] = field(default_factory=dict)
    parsed_responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    # 工具步骤审计（§三.2）：按 toolCallId 配对 start/end，随助手消息落库
    tool_steps: list[ToolStep] = field(default_factory=list)
    tool_started_at: dict[str, float] = field(default_factory=dict)
    tool_started_ms: dict[str, int] = field(default_factory=dict)
    run_started_monotonic: float = 0.0
    # 自动重试（§八.3）：已完成的尝试次数与每次的记录
    attempts: int = 1
    retry_history: list[RetryAttempt] = field(default_factory=list)
    finalized: bool = False
    finalize_scheduled: bool = False
    settled_emitted: bool = False


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _compose_prompt(text: str, document_note: str | None) -> str | None:
    """实际发给内核的 prompt：原文 + 文档注记（如有）。

    落库的用户消息保持原文，注记只进 prompt。发送与运行时镜像匹配必须以同一份
    拼接结果为基准，否则带注记的消息永远关联不上镜像，分叉时找不到 entry。
    """
    if text and document_note:
        return f"{text}\n\n{document_note}"
    return text or document_note


# 「原始响应」的单条上限。完整正文已经作为消息存过一份，日志再整存一遍只是翻倍占用；
# 超过就保留元数据、截断正文块，并如实标记（§十三.1 要的是可解释，不是字字留存）。
MAX_RESPONSE_CHARS = 200_000
# 兼容不同协议对文本块字段的命名
_TEXT_KEYS = ("text", "thinking", "content")


def _truncate_block(block: Any, budget: int) -> Any:
    if isinstance(block, str):
        return block if len(block) <= budget else f"{block[:budget]}…[截断，原长 {len(block)}]"
    if not isinstance(block, dict):
        return block
    out = dict(block)
    for key in _TEXT_KEYS:
        value = out.get(key)
        if isinstance(value, str) and len(value) > budget:
            out[key] = f"{value[:budget]}…[截断，原长 {len(value)}]"
    return out


def _parsed_response(message: dict[str, Any]) -> dict[str, Any]:
    """把 ``message_end`` 上的助手消息整理成请求日志的「原始响应」视图。

    **这是解析后的响应对象，不是 provider 的线上字节**：Pi 的
    ``after_provider_response`` 只给 status 与 headers（已核对 types.d.ts），
    扩展看不到 HTTP 正文。所以这里存 content 块、usage、stopReason、errorMessage
    ——它们才是排错时真正要看的东西，且都来自协议的标准字段（§十三.2 的同一条原则：
    不解析正文里的非标准标签）。

    脱敏走应用侧纵深那一道（``redact_body``）：模型可能回显凭据，
    而 §十三.1 明确要求「加密原始日志保存完整凭据应默认禁止」。
    """
    payload = {
        "model": message.get("model"),
        "stopReason": message.get("stopReason"),
        "usage": message.get("usage"),
        "content": message.get("content"),
        "errorMessage": message.get("errorMessage"),
    }
    redacted = redact_body(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)
    if len(serialized) <= MAX_RESPONSE_CHARS:
        return redacted if isinstance(redacted, dict) else payload

    blocks = redacted.get("content") if isinstance(redacted, dict) else None
    trimmed = dict(redacted) if isinstance(redacted, dict) else dict(payload)
    if isinstance(blocks, list) and blocks:
        budget = max(1, MAX_RESPONSE_CHARS // len(blocks))
        trimmed["content"] = [_truncate_block(block, budget) for block in blocks]
    trimmed["_truncated"] = True
    trimmed["_original_chars"] = len(serialized)
    return trimmed


def _compose_prompt(text: str, document_note: str | None) -> str | None:
    """实际发给内核的 prompt：原文 + 文档注记（如有）。

    注记只进 prompt、不落消息原文；运行时镜像记录的是内核实际收到的 prompt。
    发送与镜像匹配**必须共用这一个拼装点**，否则带注记的消息永远关联不上镜像。
    """
    if text and document_note:
        return f"{text}\n\n{document_note}"
    return text or document_note


class RouteMismatchError(RuntimeError):
    """实际内核未采用本轮路由；不得作为网络故障自动重试。"""


class RunCoordinator:
    """驱动一轮请求，并把应用权威数据按事务写下去。"""

    def __init__(
        self,
        kernel: AgentKernel | None,
        uow_factory: UnitOfWorkFactory,
        *,
        context: ContextProvider,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.memory_service: MemoryService | None = None
        self.attachment_builder: Callable[[list[str]], AttachmentPayload] | None = None
        self._transitioning = False
        self._runtime_valid = True
        self._kernel = kernel
        self._uow_factory = uow_factory
        self._context = context
        self._clock = clock or _default_clock
        self._new_id = id_factory or _default_id

        self._handlers: list[ChatEventHandler] = []
        self._permission_handler: PermissionHandler | None = None
        self._live: _LiveRun | None = None
        self._busy = False
        self._memory_cancelled = False
        self._last_run_id: str | None = None
        self._active_conversation_id: str | None = None
        self._active_branch_id: str | None = None
        self._finalize_tasks: set[asyncio.Task[None]] = set()
        self._last_tick: datetime | None = None
        self._runtime_states = RuntimeStateService(uow_factory)

        if kernel is not None:
            kernel.subscribe(self._on_kernel_event)
            kernel.set_observation_handler(self._on_observation)
            kernel.set_permission_handler(self._on_permission)

    @property
    def memory_context(self) -> MemoryRunContext | None:
        live = self._live
        if not self._busy or live is None or self._memory_cancelled:
            return None
        return MemoryRunContext(
            live.run_id, live.conversation_id, live.branch_id, live.user_message_id
        )

    def _tick(self) -> datetime:
        """单调时钟：保证同一协调器内实体时间戳严格递增。

        排序键是 ``(created_at, id)``；Windows 时钟粒度粗（~0.5ms），一轮内
        用户消息与助手消息可能同刻度，次序退化为随机 uuid 比较（曾实测偶发翻转）。
        """
        now = self._clock()
        if self._last_tick is not None and now <= self._last_tick:
            now = self._last_tick + timedelta(microseconds=1)
        self._last_tick = now
        return now

    # ---------- 只读状态 ----------

    @property
    def kernel(self) -> AgentKernel | None:
        """当前内核（供上层做能力探测，如切换会话时的 RUNTIME_RESTORE）。"""
        return self._kernel

    @property
    def available(self) -> bool:
        return self._kernel is not None

    @property
    def busy(self) -> bool:
        return self._busy or self._transitioning

    @contextlib.contextmanager
    def runtime_transition(self) -> Iterator[None]:
        """保留内核变更窗口；跨 await 阻止发送、切模型和恢复相互穿插。"""
        if self.busy:
            raise RuntimeError("内核正在处理请求或切换会话，请稍后重试")
        self._transitioning = True
        try:
            yield
        finally:
            self._transitioning = False

    @property
    def run_id(self) -> str | None:
        """当前运行中的 run_id，无则返回最近一轮。"""
        if self._live is not None:
            return self._live.run_id
        return self._last_run_id

    @property
    def conversation_id(self) -> str | None:
        return self._active_conversation_id

    @property
    def branch_id(self) -> str | None:
        return self._active_branch_id

    # ---------- 订阅与权限 ----------

    def subscribe(self, handler: ChatEventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._permission_handler = handler

    def _diagnostic_context(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "conversation_id": self.conversation_id, "branch_id": self.branch_id,
            "run_id": self.run_id,
        }
        if self._live is not None:
            fields.update(
                logical_model_id=self._live.context.logical_model_id,
                endpoint_id=self._live.context.endpoint_id,
            )
        return fields

    def _emit(self, kind: str, **data: Any) -> None:
        diagnostic = _DIAGNOSTIC_EVENTS.get(kind)
        if diagnostic is not None:
            level, label = diagnostic
            fields = self._diagnostic_context()
            # 不复制事件载荷：正文、思考、附件、工具参数和逐 token 更新都不进调试日志。
            fields.update({key: data[key] for key in (
                "conversation_id", "branch_id", "run_id", "message_id", "user_message_id",
                "tool_call_id", "phase", "is_error", "attempt", "max_attempts", "delay_ms",
            ) if key in data})
            if kind == TOOL:
                fields["tool_name"] = data.get("name")
                if data.get("is_error"):
                    level = logging.WARNING
            if kind in {ERROR, RUN_FAILED, RUN_INTERRUPTED}:
                fields["detail"] = data.get("message")
            _LOG.log(level, label, extra=fields)
        event = ChatEvent(kind=kind, data=data)
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:
                _LOG.exception("run.event_subscriber_failed", extra={
                    **self._diagnostic_context(), "event_kind": kind,
                })

    async def _on_permission(self, title: str, detail: str) -> bool:
        if self._permission_handler is None:
            return False
        try:
            return bool(await self._permission_handler(title, detail))
        except Exception:
            return False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._interrupt_stale_runs()
        if self._kernel is not None:
            await self._kernel.start()

    def _interrupt_stale_runs(self) -> None:
        """启动时收敛上次进程遗留的 running 记录。

        运行中的协调器只会在本进程内持有 ``_live``；应用重新启动后，数据库里仍为
        running 的记录不可能继续收到完成事件，必须明确标成 interrupted。
        """
        try:
            with self._uow_factory() as uow:
                stale = uow.runs.list_running()
                if not stale:
                    return
                now = self._tick()
                for run in stale:
                    uow.runs.update(
                        replace(
                            run,
                            status=RunStatus.INTERRUPTED,
                            finished_at=now,
                            stop_reason="runtime_restart",
                            error="应用上次退出时运行未完成",
                        )
                    )
                uow.commit()
            _LOG.warning("run.recovered_stale", extra={"count": len(stale)})
        except Exception as exc:
            # 遗留状态清理失败不应阻止内核启动，但必须可见。
            self._emit(ERROR, message=f"收敛上次未完成运行失败：{redact_text(str(exc))}")

    async def shutdown(self) -> None:
        await self.wait_idle()
        if self._kernel is not None:
            await self._kernel.shutdown()

    async def state(self) -> KernelState | None:
        if self._kernel is None:
            return None
        return await self._kernel.get_state()

    async def wait_idle(self, timeout: float = 15.0) -> None:
        """等待所有待落的收尾事务完成。应用退出前必须调用。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = [task for task in self._finalize_tasks if not task.done()]
            if not pending:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("RunCoordinator 的收尾事务未在限时内完成")
            await asyncio.wait(pending, timeout=remaining)

    # ---------- 会话定位 ----------

    async def resume(self, conversation_id: str, branch_id: str) -> bool:
        """续接历史也必须恢复内核，不能只改变数据库写入目标。"""
        return await self.switch_conversation(conversation_id, branch_id)

    async def new_session(self) -> bool:
        """等待旧轮收尾，再新建空内核会话；成功后才清空应用定位。"""
        if self.busy:
            return False
        try:
            with self.runtime_transition():
                await self.wait_idle()
                self._runtime_valid = False
                if self._kernel is not None:
                    await self._kernel.new_session()
                self._active_conversation_id = None
                self._active_branch_id = None
                self._live = None
                self._last_run_id = None
                if self.memory_service is not None:
                    self.memory_service.clear_approvals()
                self._runtime_valid = True
            return True
        except Exception as exc:
            self._emit(ERROR, message=f"新建会话失败：{redact_text(str(exc))}")
            return False

    def _prepare_switch(
        self,
        conversation_id: str,
        branch_id: str,
        known_has_messages: bool | None,
    ) -> _SwitchPreparation:
        """同步读取和解密只做一遍；调用方负责把本方法放在线程中。"""
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
            branch = uow.branches.get(branch_id)
            if (
                conversation is None
                or branch is None
                or branch.conversation_id != conversation_id
            ):
                return _SwitchPreparation(valid=False)
            runs = uow.runs.list_for_conversation(conversation_id)
            mirrors = uow.runtime.list_for_conversation(conversation_id)
            leaf = branch_path.branch_leaf_entry_from_records(branch, runs, mirrors)
            has_messages = (
                known_has_messages
                if known_has_messages is not None
                else bool(branch_path.branch_messages(uow, branch_id))
            )
        snapshot = (
            self._runtime_states.build_branch_snapshot_from_records(
                conversation, mirrors, leaf
            )
            if leaf is not None
            else None
        )
        return _SwitchPreparation(
            valid=True,
            has_messages=has_messages,
            snapshot=snapshot,
        )

    async def switch_conversation(
        self,
        conversation_id: str,
        branch_id: str,
        *,
        known_has_messages: bool | None = None,
    ) -> bool:
        """把内核与应用定位作为一次切换；缺少历史时拒绝沿用旧上下文。"""
        if self.busy:
            return False
        try:
            with self.runtime_transition():
                await self.wait_idle()
                prepared = await asyncio.to_thread(
                    self._prepare_switch,
                    conversation_id,
                    branch_id,
                    known_has_messages,
                )
                if not prepared.valid:
                    return False
                snapshot = prepared.snapshot
                kernel = self._kernel
                if kernel is not None:
                    if snapshot is not None and snapshot.entries:
                        if not kernel.capabilities().has(KernelCapability.RUNTIME_RESTORE):
                            raise RuntimeError("当前内核不支持恢复历史上下文，未切换会话")
                        self._runtime_valid = False
                        result = await kernel.restore_runtime_state(snapshot)
                        if result is None or not result.success:
                            raise RuntimeError(
                                getattr(result, "error", None) or "恢复运行时上下文失败"
                            )
                    elif prepared.has_messages:
                        raise RuntimeError("该分支缺少可恢复的运行时记录，未切换会话")
                    else:
                        self._runtime_valid = False
                        await kernel.new_session()
                self._active_conversation_id = conversation_id
                self._active_branch_id = branch_id
                self._live = None
                self._last_run_id = None
                if self.memory_service is not None:
                    self.memory_service.clear_approvals()
                self._runtime_valid = True
            return True
        except Exception as exc:
            self._emit(ERROR, message=f"切换会话失败：{redact_text(str(exc))}")
            return False

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
        """编辑一条用户消息：自动分叉出一个新分支，把新文本发到新分支上。

        顺序固定：fork 内核 → 建新分支（记录分叉来源）→ 定位 → 发送。
        原分支的消息**不动**——分支的语义是追加，不是改写历史。
        附件与普通发送同一套载荷（``send`` 的 ``attachment_ids`` / ``images`` /
        ``document_note``），由上层按编辑后的附件栏装配；不传即这次编辑不带附件。
        失败返回 None 并发出 error 事件；运行中拒绝。
        """
        new_text = new_text.strip()
        if not new_text and not images:
            return None
        return await self._fork_and_send(
            message_id,
            new_text,
            require_role=MessageRole.USER,
            attachment_ids=attachment_ids,
            images=images,
            document_note=document_note,
        )

    async def retry_user_message(self, message_id: str) -> str | None:
        """重试一条发送失败的用户消息。

        无论内核是否已有运行时记录，都在当前分支重发原文，不分叉。
        旧运行与部分输出保留，新尝试由标准 send 路径记为新一轮。
        """
        if self.busy:
            self._emit(ERROR, message="上一条回复还在进行中，请先等待或停止")
            return None
        location = (self._active_conversation_id, self._active_branch_id)
        # settled 会先解锁界面、再异步落库；不能让旧轮的收尾读到重试的新 entries。
        await self.wait_idle()
        if self.busy or location != (self._active_conversation_id, self._active_branch_id):
            self._emit(ERROR, message="会话状态已变化，请重新重试")
            return None
        with self._uow_factory() as uow:
            message = uow.messages.get(message_id)
            if message is None or message.role is not MessageRole.USER:
                self._emit(ERROR, message="找不到要重试的用户消息")
                return None
            if (
                message.conversation_id != self._active_conversation_id
                or self._active_branch_id is None
                or not any(
                    m.id == message_id
                    for m in branch_path.branch_messages(uow, self._active_branch_id)
                )
            ):
                self._emit(ERROR, message="只能重试当前分支中的用户消息")
                return None
            text = message.content
        try:
            payload = self._attachments_for_message(message_id)
        except Exception as exc:
            self._emit(ERROR, message=f"恢复附件失败：{redact_text(str(exc))}")
            return None
        return await self.send(
            text,
            attachment_ids=payload.attachment_ids,
            images=payload.images,
            document_note=payload.document_note,
            _retry_of_message_id=message_id,
        )

    async def regenerate(self, assistant_message_id: str) -> str | None:
        """完整回复分叉重生成；失败或停止后的回复在当前分支重试。

        内核语义：``fork`` 只接受**用户消息**的 entry（合同 §五 P0-GATE-01 实测）。
        优先从运行记录定位用户消息，旧数据则回溯同分支最近的用户消息。
        """
        if self.busy:
            self._emit(ERROR, message="上一条回复还在进行中，请先等待或停止")
            return None
        location = (self._active_conversation_id, self._active_branch_id)
        await self.wait_idle()
        if self.busy or location != (self._active_conversation_id, self._active_branch_id):
            self._emit(ERROR, message="会话状态已变化，请重新重试")
            return None
        with self._uow_factory() as uow:
            target = uow.messages.get(assistant_message_id)
            if target is None or target.role is not MessageRole.ASSISTANT:
                self._emit(ERROR, message="只能重新生成助手回复")
                return None
            # 分支对话里它之前最近的一条用户消息（含继承的前缀）
            branch_messages = branch_path.branch_messages(uow, target.branch_id)
            prior_users = [
                m
                for m in branch_messages
                if m.role is MessageRole.USER and m.created_at <= target.created_at
            ]
            if not prior_users:
                self._emit(ERROR, message="找不到可重生成的用户消息")
                return None
            run = uow.runs.get(target.run_id) if target.run_id is not None else None
            user_message = (
                uow.messages.get(run.user_message_id) if run is not None else None
            ) or prior_users[-1]
            user_text = user_message.content
            user_message_id = user_message.id
            retry_in_place = target.status in {MessageStatus.PARTIAL, MessageStatus.FAILED} or (
                run is not None
                and run.status in {RunStatus.FAILED, RunStatus.ABORTED, RunStatus.INTERRUPTED}
            )
            if retry_in_place and (
                self._active_branch_id is None
                or not any(
                    m.id == target.id
                    for m in branch_path.branch_messages(uow, self._active_branch_id)
                )
            ):
                self._emit(ERROR, message="只能重试当前分支中的助手回复")
                return None
        if retry_in_place:
            return await self.retry_user_message(user_message_id)
        try:
            payload = self._attachments_for_message(user_message_id)
        except Exception as exc:
            self._emit(ERROR, message=f"恢复附件失败：{redact_text(str(exc))}")
            return None
        return await self._fork_and_send(
            user_message_id, user_text, require_role=MessageRole.USER,
            attachment_ids=payload.attachment_ids, images=payload.images,
            document_note=payload.document_note,
        )

    def _attachments_for_message(self, message_id: str) -> AttachmentPayload:
        with self._uow_factory() as uow:
            message = uow.messages.get(message_id)
            intent = (
                uow.snapshots.get_intent(message.run_id) if message and message.run_id else None
            )
        ids = list(intent.attachment_ids) if intent else []
        if not ids:
            return AttachmentPayload()
        if self.attachment_builder is None:
            raise RuntimeError("附件服务不可用，不能仅重发文字")
        payload = self.attachment_builder(ids)
        if payload.attachment_ids != ids:
            raise RuntimeError("原消息的附件已缺失，未发送不完整请求")
        return payload

    async def switch_branch(self, branch_id: str) -> bool:
        """切换到同会话的另一分支，与会话切换共用收尾和恢复边界。"""
        if self._active_conversation_id is None:
            return False
        return await self.switch_conversation(self._active_conversation_id, branch_id)

    async def _fork_and_send(
        self,
        message_id: str,
        text: str,
        *,
        require_role: MessageRole,
        attachment_ids: list[str] | None = None,
        images: list[dict[str, Any]] | None = None,
        document_note: str | None = None,
    ) -> str | None:
        """fork 内核 → 建新分支 → 定位 → 发送。分支操作共用骨架。"""
        if self.busy:
            self._emit(ERROR, message="上一条回复还在进行中，请先等待或停止")
            return None
        if not self._runtime_valid:
            self._emit(ERROR, message="上下文切换未完成，请重新打开会话后再分叉")
            return None
        kernel = self._kernel
        if kernel is None:
            self._emit(ERROR, message="未配置可用的模型内核")
            return None
        if not kernel.capabilities().has(KernelCapability.BRANCHING):
            self._emit(ERROR, message="当前内核不支持分支")
            return None

        now = self._tick()
        with self._uow_factory() as uow:
            message = uow.messages.get(message_id)
            if message is None or message.role is not require_role:
                self._emit(ERROR, message="目标消息不存在或类型不符")
                return None
            conversation_id = message.conversation_id
            parent_branch_id = message.branch_id

            # 消息 → Pi entry：镜像按 message_id 反查
            entry_id = self._entry_for_message(uow, conversation_id, message_id)
            if entry_id is None:
                self._emit(ERROR, message="该消息没有运行时记录，无法分叉")
                return None

            # 1. 内核分叉（运行时副作用）
            try:
                with self.runtime_transition():
                    await self.wait_idle()
                    await kernel.fork(entry_id)
            except Exception as exc:
                self._emit(ERROR, message=f"分叉失败：{redact_text(str(exc))}")
                return None

            # 2. 建新分支并定位（与分叉动作成对记录，便于审计回溯）
            new_branch_id = self._new_id("branch")
            uow.branches.add(
                Branch(
                    id=new_branch_id,
                    conversation_id=conversation_id,
                    created_at=now,
                    parent_branch_id=parent_branch_id,
                    forked_from_message_id=message_id,
                )
            )
            try:
                fork_memory(uow, message_id, conversation_id, new_branch_id)
                uow.commit()
            except Exception as exc:
                uow.rollback()
                self._emit(ERROR, message=f"保存分叉记忆失败：{redact_text(str(exc))}")
                # 运行时已 fork：恢复原分支，避免下一次发送落到错误的运行时分支。
                if self._active_branch_id:
                    await self.resume(conversation_id, self._active_branch_id)
                return None

        # 3. 定位到新分支
        self._active_conversation_id = conversation_id
        self._active_branch_id = new_branch_id
        self._emit(
            BRANCHED,
            branch_id=new_branch_id,
            parent_branch_id=parent_branch_id,
            from_message_id=message_id,
        )

        # 4. 发送（走标准 send 路径，双快照/镜像照常落库）
        return await self.send(
            text,
            attachment_ids=attachment_ids,
            images=images,
            document_note=document_note,
        )

    def _entry_for_message(
        self, uow: UnitOfWork, conversation_id: str, message_id: str
    ) -> str | None:
        """应用消息 → Pi entry：镜像按 message_id 反查（不用 Pi entry ID 当主键）。"""
        snapshot = self._runtime_states.build_snapshot(conversation_id)
        allowed = {str(e["id"]) for e in snapshot.entries} if snapshot is not None else set()
        mirrors = uow.runtime.list_for_conversation(conversation_id)
        for mirror in mirrors:
            if mirror.message_id == message_id and mirror.entry_id in allowed:
                return str(mirror.entry_id)
        return self._relink_orphan_mirror(uow, mirrors, allowed, message_id)

    def _relink_orphan_mirror(
        self,
        uow: UnitOfWork,
        mirrors: list[RuntimeEntryMirror],
        allowed: set[str],
        message_id: str,
    ) -> str | None:
        """补链旧版落空的镜像：带注记的消息曾按**原文**匹配，镜像 message_id 悬空。

        只认「原文 + 意图快照注记」拼回的**实际 prompt** 严格相等、且全会话孤儿
        候选唯一；有歧义宁可继续拒绝——分叉锚点不能靠猜（旧版同文误关联的教训）。
        补链与分叉在同一事务提交，恢复原分支时也互不亏欠。
        """
        message = uow.messages.get(message_id)
        if message is None or message.role is not MessageRole.USER:
            return None
        intent = uow.snapshots.get_intent(message.run_id) if message.run_id else None
        attachment_ids = list(intent.attachment_ids) if intent else []
        if not attachment_ids or self.attachment_builder is None:
            return None
        try:
            payload = self.attachment_builder(attachment_ids)
        except Exception:
            return None  # 附件本体缺失则无法复原 prompt，不猜
        expected = _compose_prompt(message.content, payload.document_note) or ""
        candidates = [
            m
            for m in mirrors
            if m.message_id is None
            and (pm := m.payload.get("message") or {}).get("role") == "user"
            and self._extract_text(pm) == expected
        ]
        if len(candidates) != 1:
            return None
        orphan = candidates[0]
        if orphan.entry_id not in allowed:
            return None
        uow.runtime.link_message(orphan.id, message_id)
        return str(orphan.entry_id)

    def _leaf_entry_for_branch(self, branch_id: str) -> str | None:
        """分支叶子的 Pi entry（见 :func:`branch_path.branch_leaf_entry`）。"""
        with self._uow_factory() as uow:
            return branch_path.branch_leaf_entry(uow, branch_id)

    def _ensure_session(self, uow: Any, now: datetime) -> tuple[str, str, bool]:
        if self._active_conversation_id and self._active_branch_id:
            return self._active_conversation_id, self._active_branch_id, False

        conversation_id = self._new_id("conv")
        branch_id = self._new_id("branch")
        uow.conversations.add(
            Conversation(
                id=conversation_id,
                title=DEFAULT_CONVERSATION_TITLE,
                created_at=now,
                default_logical_model_id=self._context().logical_model_id,
            )
        )
        uow.branches.add(Branch(id=branch_id, conversation_id=conversation_id, created_at=now))
        self._active_conversation_id = conversation_id
        self._active_branch_id = branch_id
        return conversation_id, branch_id, True

    # ---------- 发送 ----------

    async def send(
        self,
        text: str,
        *,
        attachment_ids: list[str] | None = None,
        images: list[dict[str, Any]] | None = None,
        document_note: str | None = None,
        _retry_of_message_id: str | None = None,
    ) -> str | None:
        """发起一轮。返回 run_id；被拒绝时返回 None。

        附件（Task 4.4）：``images`` 是内核级图片载荷（``{type:"image", data, mimeType}``，
        由上层从加密 blob 仓读原图构造）；``attachment_ids`` 记进意图快照供审计与
        引用检查。``document_note`` 是给模型的文本文档清单（file_id/名称/行数），
        **只进内核 prompt，不进落库的用户消息**——模型靠它知道有附件可读。
        带图片而当前模型不支持视觉时**拒绝发送**（不静默 OCR）。
        """
        text = text.strip()
        attachment_ids = attachment_ids or []
        document_note = (document_note or "").strip()
        if not text and not images and not document_note:
            return None
        if self._kernel is None:
            self._emit(ERROR, message="未配置可用的模型内核")
            return None
        if self.busy:
            self._emit(ERROR, message="上一条回复还在进行中，请先等待或停止")
            return None

        if not self._runtime_valid:
            self._emit(ERROR, message="上下文切换未完成，请重新打开会话或新建会话后再发送")
            return None

        # 视觉能力检查（Task 4.4）：有图片但模型不支持视觉 → 拒绝
        context = deepcopy(self._context())
        if images and not context.supports_images:
            self._emit(
                ERROR,
                message="当前模型不支持图片输入。请切换到支持视觉的模型，或移除图片。未做 OCR。",
            )
            return None

        with self.runtime_transition():
            await self.wait_idle()

        now = self._tick()
        run_id = self._new_id("run")
        user_message_id = self._new_id("msg")
        assistant_message_id = self._new_id("msg")

        # 用户消息 + RunRecord + 意图快照：调用内核之前原子提交。
        # **存储故障必须变成可观测的错误**（Task 10.1）：磁盘满/库被锁时若让
        # 异常抛穿，GUI 侧只会得到「未处理的任务异常」——用户看不到任何提示，
        # 这一轮却凭空消失了。这里捕获后发 error 事件并返回 None。
        try:
            conversation_id, branch_id, first_turn = self._persist_run_start(
                run_id,
                user_message_id,
                assistant_message_id,
                text,
                context,
                now,
                attachment_ids,
                retry_of_message_id=_retry_of_message_id,
            )
        except Exception as exc:
            self._emit(ERROR, message=f"保存本轮失败（存储不可用）：{redact_text(str(exc))}")
            return None

        prompt = _compose_prompt(text, document_note)
        self._live = _LiveRun(
            run_id=run_id,
            conversation_id=conversation_id,
            branch_id=branch_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            user_text=text,
            context=context,
            sent_prompt=prompt,
            images=deepcopy(images or []),
            first_turn=first_turn,
            run_started_monotonic=time.monotonic(),
        )
        self._last_run_id = run_id
        self._busy = True
        self._memory_cancelled = False
        # message_id 随事件带出：分支操作（编辑/重生成）要以消息为锚点
        retry_data = (
            {"retry_of_message_id": _retry_of_message_id} if _retry_of_message_id else {}
        )
        self._emit(USER, text=text, message_id=user_message_id, **retry_data)

        self._transitioning = True
        try:
            await self._prepare_route(self._live)
            if self._cancel_before_prompt(self._live):
                return run_id
            if self.memory_service is not None and self.memory_context is not None:
                self.memory_service.clear_approvals()
                with self._uow_factory() as uow:
                    round_number = sum(
                        m.role is MessageRole.USER
                        for m in branch_path.branch_messages(uow, branch_id)
                    )
                await self._kernel.set_memory_context(
                    self.memory_service.prompt_context(self.memory_context, round_number)
                )
            if self._cancel_before_prompt(self._live):
                return run_id
            # 注记只进内核 prompt；落库的用户消息保持原文（审计与 UI 都看原文）。
            # 运行时镜像记录的是内核实际收到的 prompt，恢复/fork 不会丢附件线索。
            await self._kernel.send_message(prompt, images=self._live.images or None)
        except Exception as exc:
            # 内核未能接受这轮：用户消息与失败 Run 都留着，不删除用户输入
            if self._cancel_before_prompt(self._live):
                return run_id
            self._live.route_error = isinstance(exc, RouteMismatchError)
            self._live.error = redact_text(str(exc))
            self._busy = False
            self._emit(
                ERROR,
                message=f"发送失败：{self._live.error}",
                user_message_id=user_message_id,
            )
            self._emit(SETTLED)
            self._schedule_finalize(forced_status=RunStatus.FAILED)
        finally:
            self._transitioning = False
        return run_id

    def _cancel_before_prompt(self, live: _LiveRun) -> bool:
        if self._live is not live or live.finalize_scheduled or not self._busy:
            return True
        if self._memory_cancelled:
            # abort 在路由/记忆预检期间可能没有 Pi 运行可取消；仍不得随后发送 prompt。
            live.stop_reason = "aborted"
            live.error = None
            self._on_settled()
            return True
        return False

    async def _prepare_route(self, live: _LiveRun) -> None:
        """应用路由是发送的权威；fork/restore 可能重建 Pi 并恢复历史模型。"""
        model_id = live.context.app_params.get("model")
        if model_id is None:
            return  # 未装配路由的临时控制器不自行猜测模型。
        kernel = self._kernel
        assert kernel is not None
        try:
            if not isinstance(model_id, str) or not model_id:
                raise ValueError("本轮缺少实际模型 ID")
            await kernel.set_model(live.context.endpoint_id, model_id)
            if live.context.thinking_level is not None:
                await kernel.set_thinking_level(live.context.thinking_level)
            state = await kernel.get_state()
            if state.provider != live.context.endpoint_id or state.model_id != model_id:
                raise ValueError(
                    f"期望 {live.context.endpoint_id}/{model_id}，"
                    f"实际 {state.provider or '未知站点'}/{state.model_id or '未知模型'}"
                )
            if state.is_streaming or state.is_compacting:
                raise ValueError("内核尚未空闲，未发送新请求")
            if (
                live.context.thinking_level is not None
                and state.thinking_level != live.context.thinking_level
            ):
                raise ValueError(
                    f"思考强度未生效：期望 {live.context.thinking_level}，"
                    f"实际 {state.thinking_level}"
                )
        except Exception as exc:
            raise RouteMismatchError(f"发送前路由校验失败：{redact_text(str(exc))}") from exc

    def _persist_run_start(
        self,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        text: str,
        context: RunContext,
        now: datetime,
        attachment_ids: list[str],
        *,
        retry_of_message_id: str | None = None,
    ) -> tuple[str, str, bool]:
        """原子写入开场数据，返回 (会话, 分支, 是否为新会话首轮)。

        拆成独立方法是为了让 :meth:`send` 能整体捕获存储异常——
        半截状态由 UoW 的事务语义保证不会留下。
        """
        with self._uow_factory() as uow:
            conversation_id, branch_id, first_turn = self._ensure_session(uow, now)
            # 意图快照记录的是**实际上下文**：分叉分支要带上继承的前缀
            prior_ids = tuple(m.id for m in branch_path.branch_messages(uow, branch_id))
            capture_memory(uow, user_message_id, conversation_id, branch_id)
            uow.messages.add(
                Message(
                    id=user_message_id,
                    conversation_id=conversation_id,
                    branch_id=branch_id,
                    role=MessageRole.USER,
                    content=text,
                    created_at=now,
                    run_id=run_id,
                )
            )
            uow.runs.add(
                RunRecord(
                    id=run_id,
                    conversation_id=conversation_id,
                    branch_id=branch_id,
                    status=RunStatus.RUNNING,
                    created_at=now,
                    user_message_id=user_message_id,
                    retry_of_message_id=retry_of_message_id,
                )
            )
            uow.snapshots.add_intent(
                RequestIntentSnapshot(
                    id=self._new_id("intent"),
                    run_id=run_id,
                    conversation_id=conversation_id,
                    branch_id=branch_id,
                    logical_model_id=context.logical_model_id,
                    endpoint_id=context.endpoint_id,
                    routing_reason=context.routing_reason,
                    created_at=now,
                    message_ids=(*prior_ids, user_message_id),
                    attachment_ids=tuple(attachment_ids),
                    app_params=dict(context.app_params),
                )
            )
            uow.commit()
            return conversation_id, branch_id, first_turn

    async def abort(self) -> None:
        self._memory_cancelled = True
        if self.memory_service is not None:
            self.memory_service.clear_approvals()
        if self._kernel is not None:
            await self._kernel.abort()

    async def mark_interrupted(self, reason: str = "Runtime 异常退出") -> None:
        """Runtime 非正常终止时由上层调用：本轮落成 ``interrupted``，**不自动重发**。"""
        if self._live is None:
            return
        # 重试必须停：Runtime 都没了，重发没有意义
        self._live.attempts = 1

        self._live.error = redact_text(reason)
        self._busy = False
        self._emit(
            RUN_INTERRUPTED, message=self._live.error, user_message_id=self._live.user_message_id
        )
        self._emit(SETTLED)
        self._schedule_finalize(forced_status=RunStatus.INTERRUPTED)
        await self.wait_idle()

    # ---------- 内核事件翻译 ----------

    def _on_kernel_event(self, event: KernelEvent) -> None:
        kind = event.kind
        payload = event.payload
        live = self._live

        if kind == "message.start":
            if (payload.get("message") or {}).get("role") == "assistant":
                if live is not None:
                    live.text = ""
                    live.thinking = ""
                self._emit(ASSISTANT_START)
                tape = self._tape_of(live) if live is not None else None
                if tape is not None:
                    tape.add("start", offset_ms=self._offset_ms(live))

        elif kind == "message.update":
            sub = payload.get("assistantMessageEvent") or {}
            sub_type = sub.get("type")
            tape = self._tape_of(live) if live is not None else None
            if sub_type == "text_delta":
                delta = str(sub.get("delta", ""))
                if live is not None:
                    live.text += delta
                if tape is not None:
                    tape.add("text", offset_ms=self._offset_ms(live), chars=len(delta))
                self._emit(ASSISTANT_DELTA, text=delta)
            elif sub_type == "thinking_delta":
                delta = str(sub.get("delta", ""))
                if live is not None:
                    live.thinking += delta
                if tape is not None:
                    tape.add("thinking", offset_ms=self._offset_ms(live), chars=len(delta))
                self._emit(THINKING_DELTA, text=delta)
            elif sub_type is not None and tape is not None:
                # Pi 的事件类型会随版本增加：没见过的类型留一条截断预览，
                # 而不是静默忽略——否则「流里有东西但界面上没有」会变成谜。
                tape.add_unknown(str(sub_type), offset_ms=self._offset_ms(live), preview=sub)

        elif kind == "message.end":
            message = payload.get("message") or {}
            if message.get("role") == "assistant":
                final_text = self._extract_text(message)
                stop = message.get("stopReason")
                if live is not None:
                    live.stop_reason = stop
                    if final_text:
                        # message_end 是最终权威副本，覆盖增量拼接结果
                        live.text = final_text
                    if stop == "error":
                        live.error = redact_text(str(message.get("errorMessage") or "模型请求失败"))
                    tape = self._tape_of(live)
                    if tape is not None:
                        tape.add(
                            "end",
                            offset_ms=self._offset_ms(live),
                            stop=stop,
                            chars=len(final_text or ""),
                        )
                    if live.active_request_index >= 0:
                        live.parsed_responses[live.active_request_index] = _parsed_response(message)
                self._emit(
                    ASSISTANT_END,
                    text=final_text,
                    stop_reason=stop,
                    thinking=live.thinking if live is not None else "",
                    message_id=live.assistant_message_id if live is not None else None,
                    user_message_id=live.user_message_id if live is not None else None,
                )
                if stop == "error":
                    self._emit(
                        ERROR,
                        message=live.error if live is not None else "模型请求失败",
                        user_message_id=live.user_message_id if live is not None else None,
                    )

        elif kind == "tool.start":
            self._record_tool_start(live, payload)
            tape = self._tape_of(live) if live is not None else None
            if tape is not None:
                tape.add(
                    "tool.start",
                    offset_ms=self._offset_ms(live),
                    name=payload.get("toolName"),
                )
            self._emit(
                TOOL,
                name=payload.get("toolName"),
                phase="start",
                tool_call_id=payload.get("toolCallId"),
            )
        elif kind == "tool.end":
            self._record_tool_end(live, payload)
            tape = self._tape_of(live) if live is not None else None
            if tape is not None:
                tape.add(
                    "tool.end",
                    offset_ms=self._offset_ms(live),
                    name=payload.get("toolName"),
                    error=bool(payload.get("isError", False)),
                )
            self._emit(
                TOOL,
                name=payload.get("toolName"),
                phase="end",
                is_error=bool(payload.get("isError", False)),
                tool_call_id=payload.get("toolCallId"),
            )

        elif kind == "run.settled":
            self._on_settled()

        elif kind == "runtime.exited":
            # 适配器上报进程意外退出：本轮落 interrupted，不重发
            self._emit(
                RUN_INTERRUPTED,
                message="Runtime 异常退出",
                user_message_id=live.user_message_id if live is not None else None,
            )
            if self._live is not None:
                self._live.error = "Runtime 异常退出"
            self._busy = False
            self._emit(SETTLED)
            self._schedule_finalize(forced_status=RunStatus.INTERRUPTED)

        elif kind == "extension.error":
            self._emit(ERROR, message=redact_text(str(payload.get("error", "扩展错误"))))

    def _on_settled(self) -> None:
        live = self._live
        if live is not None:
            # 幂等：重复的 settled 不再向 GUI 发第二次收敛通知，也不重复排定收尾
            if live.settled_emitted:
                return
            live.settled_emitted = True
            # 自动重试（§八.3）：只有**连接类**错误且还有次数时才重试。
            # 判定要在收尾之前——收尾会把本轮定稿成 failed。
            if self._should_retry(live):
                self._schedule_retry(live)
                return
        self._busy = False
        self._emit(SETTLED)
        if (
            live is not None
            and live.first_turn
            and live.text.strip()
            and live.error is None
            and live.stop_reason not in {"aborted", "error"}
        ):
            self._emit(
                FIRST_RESPONSE_COMPLETED,
                conversation_id=live.conversation_id,
                user_text=live.user_text,
                assistant_text=live.text,
            )
        self._schedule_finalize()

    # ---------- 自动重试（§八.3） ----------

    def _should_retry(self, live: _LiveRun) -> bool:
        """是否该重试这一轮。**分类来自领域规则，这里只做编排判断。**"""
        if live.error is None or live.route_error:
            return False
        policy = self._retry_policy()
        error_class = self._classify_failure(live)
        return policy.should_retry(attempt=live.attempts, error_class=error_class)

    def _retry_policy(self) -> RetryPolicy:
        context = self._live.context if self._live is not None else self._context()
        return context.retry_policy or RetryPolicy()

    def _classify_failure(self, live: _LiveRun) -> ErrorClass:
        """把本轮失败归类。已跑过工具 → 一律不可重试（领域规则保证）。"""
        status = live.responses[-1].get("status") if live.responses else None
        return classify(
            message=live.error or "",
            status=status if isinstance(status, int) else None,
            had_content=bool(live.text.strip()),
            tools_ran=bool(live.tool_steps),
        )

    def _schedule_retry(self, live: _LiveRun) -> None:
        """排一次重试：记尝试 → 通知界面 → 退避 → 重发。

        重发走**同一个 run**（不新建 run）：那是同一次用户请求的续试，
        不是新的一轮。请求日志里靠传输快照的 ``attempt`` 区分。
        """
        error_class = self._classify_failure(live)
        attempt = RetryAttempt(
            attempt=live.attempts,
            error_class=error_class,
            message=live.error or "",
            status=live.responses[-1].get("status") if live.responses else None,
        )
        live.retry_history.append(attempt)
        delay_ms = self._retry_policy().delay_ms(attempt=live.attempts)
        # 记在**失败那次请求**的磁带上：排错时要看的是「这次流断在哪、等了多久」，
        # 而不是新一次尝试的开头。
        tape = self._tape_of(live)
        if tape is not None:
            tape.add(
                "retry",
                offset_ms=self._offset_ms(live),
                attempt=live.attempts,
                klass=error_class.value,
                delay_ms=delay_ms,
            )
        live.attempts += 1

        # 重试前只清「本轮结果」：文本/思考/错误要从头累积（否则内容会重复）。
        # **观测不清**——失败尝试的请求与响应要留在传输快照里，
        # 那正是 §十三.1 要求的「重试记录」。
        live.text = ""
        live.thinking = ""
        live.error = None
        live.stop_reason = None
        live.settled_emitted = False

        # 在消息内显示（§八.3 明确要求）
        self._emit(
            "retrying",
            attempt=live.attempts,
            error_class=error_class.value,
            reason=attempt.describe,
            delay_ms=delay_ms,
            max_attempts=self._retry_policy().max_attempts,
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有事件循环（同步测试场景）：不重试，直接收尾成失败
            self._busy = False
            self._emit(SETTLED)
            self._schedule_finalize(forced_status=RunStatus.FAILED)
            return

        async def _retry() -> None:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            if self._kernel is None or self._cancel_before_prompt(live):
                return  # 期间被取消/切走
            self._transitioning = True
            try:
                await self._prepare_route(live)
                if self._cancel_before_prompt(live):
                    return
                await self._kernel.send_message(live.user_text, images=live.images or None)
            except Exception as exc:
                if self._cancel_before_prompt(live):
                    return
                live.route_error = isinstance(exc, RouteMismatchError)
                live.error = redact_text(str(exc))
                self._on_settled()  # 再走一次判定（可能还有次数）
            finally:
                self._transitioning = False

        task = loop.create_task(_retry())
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    # ---------- provider 观测 ----------

    def _tape_of(self, live: _LiveRun) -> StreamTape | None:
        """当前这次 provider 请求的磁带。没有请求记录时返回 None（无从归属）。"""
        if live.active_request_index < 0:
            return None
        return live.tapes.setdefault(live.active_request_index, StreamTape())

    @staticmethod
    def _offset_ms(live: _LiveRun | None) -> int:
        """相对本轮开始的毫秒偏移。没有活动轮次时返回 0（那时也不会有磁带）。"""
        if live is None:
            return 0
        started = live.run_started_monotonic or time.monotonic()
        return max(0, int((time.monotonic() - started) * 1000))

    def _on_observation(self, payload: dict[str, Any]) -> None:
        live = self._live
        if live is None:
            return
        kind = payload.get("kind")
        # 每次观测都记下当时是第几次尝试（§十三.1 的「重试记录」靠它区分）
        attempt = live.attempts
        if kind == "provider.request":
            live.requests.append(
                {
                    "payload": payload.get("payload") or {},
                    "provider": payload.get("provider"),
                    "model": payload.get("model"),
                    "url": payload.get("url"),
                    "_attempt": attempt,
                }
            )
            # 观测到一次真实请求：后续的流式事件都归它（重试会产生新的序号）
            live.active_request_index = len(live.requests) - 1
        elif kind == "provider.headers":
            live.request_headers.append(payload.get("headers") or {})
        elif kind == "provider.response":
            live.responses.append(
                {
                    "status": payload.get("status"),
                    "headers": payload.get("headers") or {},
                }
            )
            tape = self._tape_of(live)
            if tape is not None:
                tape.add("http", offset_ms=self._offset_ms(live), status=payload.get("status"))

    # ---------- 工具步骤（§三.2） ----------

    def _record_tool_start(self, live: _LiveRun | None, payload: dict[str, Any]) -> None:
        """记一个工具步骤的开始。按 toolCallId 配对（Pi 的 start/end 都带它）。"""
        if live is None:
            return
        call_id = str(payload.get("toolCallId") or "")
        if not call_id:
            return
        started = time.monotonic()
        live.tool_started_at[call_id] = started
        live.tool_started_ms[call_id] = int(
            (started - (live.run_started_monotonic or started)) * 1000
        )
        live.tool_steps.append(
            ToolStep(
                tool_call_id=call_id,
                name=str(payload.get("toolName") or "tool"),
                created_at_ms=live.tool_started_ms[call_id],
                args=dict(payload.get("args") or {}),
            )
        )

    def _record_tool_end(self, live: _LiveRun | None, payload: dict[str, Any]) -> None:
        """用结束事件补全对应步骤（状态、结果摘要、耗时、错误）。"""
        if live is None:
            return
        call_id = str(payload.get("toolCallId") or "")
        started = live.tool_started_at.pop(call_id, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started else 0
        is_error = bool(payload.get("isError", False))
        result = payload.get("result")
        for index, step in enumerate(live.tool_steps):
            if step.tool_call_id == call_id:
                live.tool_steps[index] = finish_tool_step(
                    step,
                    result=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                )
                return
        # 没有配对的 start（迟到事件）：补一条完整记录，不丢审计
        live.tool_steps.append(
            finish_tool_step(
                ToolStep(
                    tool_call_id=call_id,
                    name=str(payload.get("toolName") or "tool"),
                    created_at_ms=int(
                        (time.monotonic() - (live.run_started_monotonic or 0)) * 1000
                    ),
                ),
                result=result,
                is_error=is_error,
                duration_ms=duration_ms,
            )
        )

    # ---------- 收尾 ----------

    def _schedule_finalize(self, *, forced_status: RunStatus | None = None) -> None:
        live = self._live
        if live is None or live.finalize_scheduled or live.finalized:
            return
        live.finalize_scheduled = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._finalize_sync(live, forced_status)
            return
        task = loop.create_task(self._finalize(live, forced_status))
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    def _finalize_sync(self, live: _LiveRun, forced_status: RunStatus | None) -> None:
        """无事件循环时的收尾路径（同步测试场景），不做 Pi entries 拉取。

        同样捕获存储故障——调用方（``send`` 的异常路径）不该因为收尾失败而炸。
        """
        try:
            self._commit_final(live, forced_status, [])
        except Exception as exc:
            self._emit(
                ERROR,
                message=f"保存本轮结果失败（存储不可用）：{redact_text(str(exc))}",
            )

    async def _finalize(self, live: _LiveRun, forced_status: RunStatus | None) -> None:
        """落库收尾。**存储故障不得静默**（Task 10.1）。

        收尾跑在独立任务里，异常不会被谁 await 到——若不捕获，Python 只会打一条
        「Task exception was never retrieved」的警告，用户界面毫无提示。
        这里捕获后发 error 事件，把「这一轮没存下来」如实告诉用户。
        """
        mirrors: list[RuntimeEntryMirror] = []
        if self._kernel is not None:
            try:
                entries = await self._kernel.get_entries()
            except Exception:
                entries = []
            mirrors = self._build_mirrors(live, entries)
        try:
            self._commit_final(live, forced_status, mirrors)
        except Exception as exc:
            self._emit(
                ERROR,
                message=f"保存本轮结果失败（存储不可用）：{redact_text(str(exc))}",
            )

    def _commit_final(
        self,
        live: _LiveRun,
        forced_status: RunStatus | None,
        mirrors: list[RuntimeEntryMirror],
    ) -> None:
        if live.finalized:
            return
        live.finalized = True

        now = self._tick()
        status, message_status = self._classify(live, forced_status)

        with self._uow_factory() as uow:
            run = uow.runs.get(live.run_id)
            if run is None:
                return

            assistant_id: str | None = None
            if live.text:
                uow.messages.add(
                    Message(
                        id=live.assistant_message_id,
                        conversation_id=live.conversation_id,
                        branch_id=live.branch_id,
                        role=MessageRole.ASSISTANT,
                        content=live.text,
                        created_at=now,
                        run_id=live.run_id,
                        status=message_status,
                        thinking=live.thinking,
                        tool_steps=tuple(live.tool_steps),
                    )
                )
                assistant_id = live.assistant_message_id

            for snapshot in self._build_transports(live, now):
                uow.snapshots.add_transport(snapshot)

            uow.runtime.add_many(mirrors)

            uow.runs.update(
                replace(
                    run,
                    status=status,
                    finished_at=now,
                    assistant_message_id=assistant_id,
                    stop_reason=live.stop_reason,
                    error=live.error,
                )
            )
            uow.commit()

        _LOG.info("run.finalized", extra={
            "run_id": live.run_id, "conversation_id": live.conversation_id,
            "branch_id": live.branch_id, "status": status.value,
            "duration_ms": max(0, round((time.monotonic() - live.run_started_monotonic) * 1000))
            if live.run_started_monotonic is not None else None,
        })
        if status is RunStatus.INTERRUPTED:
            self._emit(
                RUN_INTERRUPTED,
                message=live.error or "Runtime 异常退出",
                user_message_id=live.user_message_id,
            )
        elif status is RunStatus.FAILED:
            self._emit(RUN_FAILED, message=live.error or "运行失败")
        if self._live is live:
            self._live = None

    @staticmethod
    def _classify(
        live: _LiveRun, forced_status: RunStatus | None
    ) -> tuple[RunStatus, MessageStatus]:
        if forced_status is not None:
            message_status = (
                MessageStatus.PARTIAL
                if forced_status is RunStatus.INTERRUPTED and live.text
                else MessageStatus.FAILED
            )
            return forced_status, message_status
        stop = live.stop_reason
        if stop == "aborted":
            return RunStatus.ABORTED, MessageStatus.PARTIAL
        if stop == "error":
            return RunStatus.FAILED, MessageStatus.FAILED
        return RunStatus.COMPLETED, MessageStatus.COMPLETE

    def _build_transports(self, live: _LiveRun, now: datetime) -> list[TransportSnapshot]:
        """把 provider 观测转成脱敏后的传输快照。一次 run 可有多条（工具往返）。"""
        context = live.context
        out: list[TransportSnapshot] = []
        for index, request in enumerate(live.requests):
            raw_body = request.get("payload") or {}
            body = redact_body(raw_body)
            headers = redact_headers(
                live.request_headers[index] if index < len(live.request_headers) else {}
            )
            response = live.responses[index] if index < len(live.responses) else {}
            tape = live.tapes.get(index)
            out.append(
                TransportSnapshot(
                    id=self._new_id("transport"),
                    run_id=live.run_id,
                    created_at=now,
                    sequence=index + 1,
                    attempt=int(request.get("_attempt", 1)),
                    provider=request.get("provider"),
                    model_id=body.get("model") or request.get("model"),
                    url=request.get("url"),
                    headers=headers,
                    body=body,
                    param_diff=diff_params(context.app_params, body),
                    response_status=response.get("status"),
                    stop_reason=live.stop_reason,
                    error_class="provider_error" if live.stop_reason == "error" else None,
                    response_body=live.parsed_responses.get(index) or {},
                    stream_tape=tape.to_dict() if tape is not None else {},
                )
            )
        return out

    def _build_mirrors(
        self, live: _LiveRun, entries: list[dict[str, Any]]
    ) -> list[RuntimeEntryMirror]:
        """镜像尚未记录的 Pi 条目。以 ``entry_id`` 去重，避免每轮重复堆积。"""
        with self._uow_factory() as uow:
            known = {m.entry_id for m in uow.runtime.list_for_conversation(live.conversation_id)}
            conversation = uow.conversations.get(live.conversation_id)
        entries = isolate_conversation_entries(entries, conversation)
        now = self._tick()
        mirrors: list[RuntimeEntryMirror] = []
        for entry in entries:
            entry_id = entry.get("id")
            if not entry_id or entry_id in known:
                continue
            known.add(entry_id)
            message = entry.get("message") or {}
            mirrors.append(
                RuntimeEntryMirror(
                    id=self._new_id("mirror"),
                    conversation_id=live.conversation_id,
                    entry_id=str(entry_id),
                    entry_type=str(entry.get("type", "unknown")),
                    captured_at=now,
                    run_id=live.run_id,
                    parent_entry_id=entry.get("parentId"),
                    message_id=self._match_message_id(live, message),
                    # 镜像内容同样脱敏：Pi 条目里可能带上工具参数中的凭据
                    payload=redact_body(entry),
                )
            )
        return mirrors

    @staticmethod
    def _match_message_id(live: _LiveRun, message: dict[str, Any]) -> str | None:
        """把 Pi 条目关联回应用消息。对不上就留空——**不用 Pi entry ID 当应用主键**。

        用户条目以**实际发出的 prompt** 为基准：注记只进 prompt、不落消息原文，
        用原文比会让带附件的消息永远关联不上，分叉时找不到 entry。
        """
        role = message.get("role")
        if role == "user":
            baseline = live.sent_prompt if live.sent_prompt is not None else ""
            if RunCoordinator._extract_text(message) == baseline:
                return live.user_message_id
        if role == "assistant" and live.text and RunCoordinator._extract_text(message) == live.text:
            return live.assistant_message_id
        return None

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return ""
