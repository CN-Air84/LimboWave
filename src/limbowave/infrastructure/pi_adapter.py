"""PiKernelAdapter：把 Pi 的 RPC 协议映射到 AgentKernel 抽象接口。

设计依据：docs/architecture/adr-0001-agent-kernel.md 第九节、合同文档 §三。

职责边界：
- 只做"协议翻译 + 进程生命周期"，不做权限决策（那是 PolicyEnforcementExtension 的事），
  不做路由/回退决策（那是应用层的事）。
- 事件在这里被归一为抽象的 ``KernelEvent.kind``，应用层只认抽象名字。
- 能力集由 ``capabilities()`` 如实上报，源自 P0 闸门的实机结论。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

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
from limbowave.infrastructure.pi_rpc import PiRpcProcess, SpawnSpec, resolve_pi_argv

# 政策执行扩展的默认路径（随应用分发）
_POLICY_EXTENSION = Path(__file__).resolve().parent / "extensions" / "policy_enforcement.ts"

# 抽象事件名 → 说明。Pi 的具体事件类型 → 抽象 kind 的映射见 _MAP。
_EVENT_MAP: dict[str, str] = {
    "agent_start": "run.start",
    "agent_end": "run.end",
    "agent_settled": "run.settled",
    "turn_start": "turn.start",
    "turn_end": "turn.end",
    "message_start": "message.start",
    "message_update": "message.update",
    "message_end": "message.end",
    "tool_execution_start": "tool.start",
    "tool_execution_update": "tool.update",
    "tool_execution_end": "tool.end",
    "queue_update": "queue.update",
    "compaction_start": "compaction.start",
    "compaction_end": "compaction.end",
    "auto_retry_start": "retry.start",
    "auto_retry_end": "retry.end",
    "extension_error": "extension.error",
    "extension_ui_request": "ui.request",
}


class PiKernelAdapter(AgentKernel):
    """对接 Pi 的 AgentKernel 实现。

    传入一个已配置好的 ``SpawnSpec``（含 ``--mode rpc --no-session --no-approve
    --no-context-files -e <PolicyEnforcementExtension>`` 等，见合同 §八）。
    """

    def __init__(self, spec: SpawnSpec, *, capabilities: KernelCapabilities | None = None) -> None:
        self._capabilities = capabilities or _default_capabilities()
        self._handlers: list[EventHandler] = []
        self._event_queue: asyncio.Queue[KernelEvent] = asyncio.Queue()
        self._ui_handler: Callable[[dict[str, Any]], Any] | None = None
        self._observation_handler: Callable[[dict[str, Any]], None] | None = None
        self._ui_tasks: set[asyncio.Task[None]] = set()

        # 包装 stderr 处理器：解析 PolicyEnforcementExtension 的 [LIMBOWAVE] 结构化日志，
        # 其余原样透传给上层。
        original_stderr = spec.stderr_handler

        def _stderr(text: str) -> None:
            prefix = "[LIMBOWAVE] "
            if text.startswith(prefix):
                import json as _json

                with contextlib.suppress(Exception):
                    self._on_observation(_json.loads(text[len(prefix) :].strip()))
            if original_stderr is not None:
                original_stderr(text)

        spec.stderr_handler = _stderr
        self._rpc = PiRpcProcess(spec)
        self._rpc.on_event(self._on_raw_event)

    # ---------- 应用接入点 ----------

    def set_ui_handler(self, handler: Callable[[dict[str, Any]], Any] | None) -> None:
        """设置扩展 UI 请求的裁决回调。

        回调收到 ``extension_ui_request``（含 method/title/message/options），
        返回应答体（如 ``{"confirmed": True}`` 或 ``{"cancelled": True}``）。
        未设置时默认拒绝（合同 §十一 GATE-04 的「默认拒绝」）。
        """
        self._ui_handler = handler

    def set_observation_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        """设置观测回调：接收扩展经 stderr 回写的结构化日志。

        包括 ``provider.request``（最终传输快照）、``provider.response``、
        ``tool_call``、``user_bash``、``compaction.request`` 等。
        """
        self._observation_handler = handler

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """AgentKernel 的权限裁决入口。翻译为扩展 UI 应答。

        政策扩展只用 ``confirm``；这里把 ``confirm`` 请求归一为 (title, detail)，
        处理器的 bool 结论映射回 ``{"confirmed": ...}``。非 confirm 的方法一律取消。
        未设置处理器时，``_ui_handler`` 为空，默认拒绝。
        """
        if handler is None:
            self._ui_handler = None
            return

        async def _ui(request: dict[str, Any]) -> dict[str, Any]:
            if request.get("method") != "confirm":
                return {"cancelled": True}
            title = str(request.get("title", ""))
            detail = str(request.get("message", ""))
            try:
                allowed = await handler(title, detail)
            except Exception:
                return {"confirmed": False}
            return {"confirmed": bool(allowed)}

        self._ui_handler = _ui

    # ---------- 能力 ----------

    def capabilities(self) -> KernelCapabilities:
        return self._capabilities

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self._rpc.start()

    async def shutdown(self) -> None:
        await self._rpc.shutdown()

    # ---------- 消息 ----------

    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        command: dict[str, Any] = {"type": "prompt", "message": text}
        if images:
            command["images"] = images
        response = await self._rpc.request(command)
        if response.get("success") is not True:
            raise RuntimeError(f"prompt 被拒绝：{response.get('error', 'unknown')}")

    async def abort(self) -> None:
        # 合同 §三.7：abort 等待会话进入 idle 后才响应
        await self._rpc.request({"type": "abort"}, timeout=120)

    # ---------- 状态 ----------

    async def get_state(self) -> KernelState:
        response = await self._rpc.request({"type": "get_state"})
        data = response.get("data") or {}
        model = data.get("model") or {}
        return KernelState(
            model_id=model.get("id") if model else None,
            thinking_level=str(data.get("thinkingLevel", "off")),
            is_streaming=bool(data.get("isStreaming", False)),
            is_compacting=bool(data.get("isCompacting", False)),
            session_id=str(data.get("sessionId", "")),
            session_name=data.get("sessionName"),
            message_count=int(data.get("messageCount", 0)),
            pending_message_count=int(data.get("pendingMessageCount", 0)),
            raw=data,
        )

    async def set_model(self, provider: str, model_id: str) -> None:
        response = await self._rpc.request(
            {"type": "set_model", "provider": provider, "modelId": model_id}
        )
        if response.get("success") is not True:
            raise RuntimeError(f"set_model 失败：{response.get('error', 'unknown')}")

    async def set_thinking_level(self, level: str) -> None:
        response = await self._rpc.request({"type": "set_thinking_level", "level": level})
        if response.get("success") is not True:
            raise RuntimeError(f"set_thinking_level 失败：{response.get('error', 'unknown')}")

    # ---------- 会话树 ----------

    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        command: dict[str, Any] = {"type": "get_entries"}
        if since is not None:
            command["since"] = since
        response = await self._rpc.request(command)
        data = response.get("data") or {}
        return list(data.get("entries") or [])

    async def fork(self, entry_id: str) -> str:
        response = await self._rpc.request({"type": "fork", "entryId": entry_id})
        if response.get("success") is not True:
            raise RuntimeError(f"fork 失败：{response.get('error', 'unknown')}")
        return str((response.get("data") or {}).get("text", ""))

    # ---------- 压缩 ----------

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult:
        command: dict[str, Any] = {"type": "compact"}
        if custom_instructions is not None:
            command["customInstructions"] = custom_instructions
        response = await self._rpc.request(command, timeout=300)
        if response.get("success") is not True:
            raise RuntimeError(f"compact 失败：{response.get('error', 'unknown')}")
        data = response.get("data") or {}
        return CompactionResult(
            summary=str(data.get("summary", "")),
            tokens_before=int(data.get("tokensBefore", 0)),
            raw=data,
        )

    # ---------- 事件 ----------

    def _on_raw_event(self, raw: dict[str, Any]) -> None:
        kind = _EVENT_MAP.get(str(raw.get("type")), f"raw.{raw.get('type', 'unknown')}")
        # 扩展 UI 请求需要应答——异步裁决，不阻塞事件泵
        if kind == "ui.request":
            self._schedule_ui_answer(raw)
        event = KernelEvent(kind=kind, payload=raw)
        # 事件进入队列（供异步迭代）与同步回调（供订阅）双通道
        self._event_queue.put_nowait(event)
        for handler in list(self._handlers):
            # 订阅者异常不得拖垮事件泵
            with contextlib.suppress(Exception):
                handler(event)

    def _on_observation(self, payload: dict[str, Any]) -> None:
        if self._observation_handler is not None:
            with contextlib.suppress(Exception):
                self._observation_handler(payload)

    def _schedule_ui_answer(self, request: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._answer_ui(request))
        # 持有引用，避免被 GC 回收导致任务提前取消
        self._ui_tasks.add(task)
        task.add_done_callback(self._ui_tasks.discard)

    async def _answer_ui(self, request: dict[str, Any]) -> None:
        req_id = request.get("id")
        method = request.get("method")
        answer: dict[str, Any] = {"type": "extension_ui_response", "id": req_id}
        if self._ui_handler is not None:
            try:
                result = self._ui_handler(request)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, dict):
                    answer.update(result)
                else:
                    answer["cancelled"] = True
            except Exception:
                answer["cancelled"] = True  # 应用异常 → 默认拒绝
        else:
            # 无 UI handler：默认拒绝
            if method == "confirm":
                answer["confirmed"] = False
            else:
                answer["cancelled"] = True
        with contextlib.suppress(Exception):
            await self._rpc.send(answer)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe

    async def events(self) -> AsyncIterator[KernelEvent]:
        while True:
            yield await self._event_queue.get()


def _default_capabilities() -> KernelCapabilities:
    """Pi 0.86.0 经 P0 闸门实机确认的能力集。

    - in_memory_session：GATE-01 通过（--no-session 下树与上下文完整存在）
    - branching：GATE-01（fork/clone 可用）
    - abort_preserves_partial：合同 §三.7（stopReason=aborted 保留部分输出）
    - final_request_hook：GATE-02 通过（before_provider_request 可观测可改写）
    - custom_compaction：GATE-03 通过（session_before_compact 可接管）
    - tool_preflight_hook：GATE-04 可绕过（tool_call 覆盖模型调用；user_bash/user_editor 需另钩）
    - internal_retry_disable：GATE-05 通过（retry.enabled=false 可关）
    - telemetry_disable：GATE-05 通过（enableInstallTelemetry=false + 启动期零出网）

    注意：persistent_session 不在其中——按裁决 3 我们用 --no-session。
    """
    return KernelCapabilities(
        capabilities=frozenset(
            {
                KernelCapability.IN_MEMORY_SESSION,
                KernelCapability.BRANCHING,
                KernelCapability.ABORT_PRESERVES_PARTIAL,
                KernelCapability.FINAL_REQUEST_HOOK,
                KernelCapability.CUSTOM_COMPACTION,
                KernelCapability.TOOL_PREFLIGHT_HOOK,
                KernelCapability.INTERNAL_RETRY_DISABLE,
                KernelCapability.TELEMETRY_DISABLE,
            }
        )
    )


def build_spawn_spec(
    *,
    provider: str,
    model_id: str,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    session_dir: Path | None = None,
    policy_extension: Path | None = None,
    stderr_handler: Callable[[str], None] | None = None,
    extra_args: list[str] | None = None,
    node: str | None = None,
    cli_path: Path | None = None,
) -> SpawnSpec:
    """组装 Pi 的启动规格（硬化基线，对应合同 §八 与 ADR 第七节）。

    基线：
    - ``--mode rpc``：显式，不依赖 TTY 自动降级（合同 §二.3 陷阱）。
    - ``--no-session``：禁止 Pi 持久化权威数据（裁决 3 / GATE-01）。
    - ``--no-approve`` + ``--no-context-files``：禁止项目本地资源与隐式提示词注入（裁决 3）。
    - ``-e <policy_enforcement.ts>``：政策执行扩展（裁决 4 / GATE-04）。
    - 环境变量关遥测与版本检查（GATE-05）。
    """
    import os

    extension = policy_extension or _POLICY_EXTENSION
    args = [
        "--mode",
        "rpc",
        "--no-session",
        "--no-approve",
        "--no-context-files",
        "--provider",
        provider,
        "--model",
        f"{provider}/{model_id}",
        "-e",
        str(extension).replace("\\", "/"),  # jiti 在 Windows 上对反斜杠路径不稳
    ]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    if extra_args:
        args += extra_args

    # env 是"叠加"语义：在 os.environ 基础上覆盖，而非整体替换。
    # 整体替换会清空 PATH/SystemRoot，Windows 上 Node 根本起不来。
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    full_env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    full_env.setdefault("PI_OFFLINE", "1")

    return SpawnSpec(
        argv=resolve_pi_argv(node=node, cli_path=cli_path, extra_args=args),
        cwd=str(cwd) if cwd is not None else None,
        env=full_env,
        stderr_handler=stderr_handler,
    )
