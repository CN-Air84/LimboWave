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
import json
import logging
import os
import secrets
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from limbowave.application.kernel import (
    AgentKernel,
    CompactionResult,
    ContextUsage,
    EventHandler,
    KernelCapabilities,
    KernelCapability,
    KernelEvent,
    KernelState,
    PermissionHandler,
)
from limbowave.domain.redaction import redact_text
from limbowave.domain.runtime_state import (
    EntryFingerprint,
    FingerprintSet,
    RestoreFailureReason,
    RuntimeRestoreResult,
    RuntimeStateSnapshot,
    RuntimeValidationResult,
    fingerprint_from_entry,
)
from limbowave.infrastructure.pi_rpc import PiRpcProcess, SpawnSpec, resolve_pi_argv

_LOG = logging.getLogger(__name__)

# 政策执行扩展的默认路径（随应用分发）
_POLICY_EXTENSION = Path(__file__).resolve().parent / "extensions" / "policy_enforcement.ts"

# 政策扩展的内部命令与口令（与 policy_enforcement.ts 同源，改动必须同步）
_RELOAD_COMMAND = "limbowave-reload-models"
CONTROL_TOKEN_ENV = "LIMBOWAVE_CONTROL_TOKEN"


def _materialize_restore_file(
    session_dir: Path,
    cwd: str,
    entries: list[dict[str, Any]],
) -> Path:
    """序列化并写入恢复文件；可能较大，必须在线程中调用。"""
    from datetime import UTC, datetime

    header = {
        "type": "session",
        "version": 3,
        "id": uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": cwd,
    }
    content = "\n".join(
        [json.dumps(header, ensure_ascii=False)]
        + [json.dumps(entry, ensure_ascii=False) for entry in entries]
    ) + "\n"
    session_dir.mkdir(parents=True, exist_ok=True)
    restore_file = session_dir / f"pi-restore-{uuid4().hex[:8]}.jsonl"
    restore_file.write_text(content, encoding="utf-8")
    return restore_file


def _validate_entries(
    expected: RuntimeStateSnapshot,
    entries: list[dict[str, Any]],
) -> RuntimeValidationResult:
    """计算恢复指纹；大历史的哈希不能占住 Qt 事件循环。"""
    entry_ids = [str(entry.get("id", "")) for entry in entries]
    id_to_position = {entry_id: i for i, entry_id in enumerate(entry_ids)}
    actual_fps = []
    for i, entry in enumerate(entries):
        fingerprint = fingerprint_from_entry(entry, branch_position=i)
        parent_id = entry.get("parentId")
        if parent_id and str(parent_id) in id_to_position:
            fingerprint = EntryFingerprint(
                entry_type=fingerprint.entry_type,
                role=fingerprint.role,
                content_hash=fingerprint.content_hash,
                parent_position=id_to_position[str(parent_id)],
                branch_position=fingerprint.branch_position,
            )
        actual_fps.append(fingerprint)
    comparison = FingerprintSet(expected=expected.fingerprint, actual=actual_fps)
    has_system = any(
        isinstance(entry.get("message"), dict)
        and (entry.get("message") or {}).get("role") == "system"
        for entry in entries
    )
    return RuntimeValidationResult(
        valid=comparison.all_match,
        fingerprint_comparison=comparison,
        has_system_entry=has_system,
        last_stable_entry_id=entry_ids[-1] if entry_ids else None,
    )


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
        self._memory_ack: asyncio.Future[None] | None = None
        self._memory_run_id: str | None = None
        self._observation_handler: Callable[[dict[str, Any]], None] | None = None
        self._ui_tasks: set[asyncio.Task[None]] = set()
        self._runtime_instance_id: str | None = None

        # 包装 stderr 处理器：解析 PolicyEnforcementExtension 的 [LIMBOWAVE] 结构化日志，
        # 其余原样透传给上层。
        original_stderr = spec.stderr_handler

        def _stderr(text: str) -> None:
            prefix = "[LIMBOWAVE] "
            if text.startswith(prefix):
                try:
                    payload = json.loads(text[len(prefix) :].strip())
                    if not isinstance(payload, dict):
                        raise ValueError("Observation is not an object")
                    self._on_observation(payload)
                except Exception as exc:
                    _LOG.warning("pi.invalid_observation", extra={"error_type": type(exc).__name__})
            if original_stderr is not None:
                original_stderr(text)

        spec.stderr_handler = _stderr
        # 内部控制命令的口令：用户在输入框里敲同名斜杠命令也调不动它
        self._control_token = secrets.token_hex(16)
        base_env = spec.env if spec.env is not None else dict(os.environ)
        spec.env = {**base_env, CONTROL_TOKEN_ENV: self._control_token}
        # 本适配器物化的会话文件目录（进程退出时整体清理）
        self._session_dir = Path(spec.cwd or ".") / "runtime" / "pi-sessions" / uuid4().hex[:12]
        self._rpc = PiRpcProcess(spec)
        self._spec = spec
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
        # 每次进程启动分配新的实例 ID，用于区分旧/新进程的事件
        if self._runtime_instance_id is None:
            self._runtime_instance_id = uuid4().hex[:16]

    async def shutdown(self) -> None:
        await self._rpc.shutdown()
        self._discard_session_files()

    def _discard_session_files(self) -> None:
        """清掉本适配器物化过的会话文件（含 Pi 分叉时在同目录派生的文件）。

        只能在 Pi 进程退出后做：会话存活期间 Pi 一直往这些文件里追加。
        """
        shutil.rmtree(self._session_dir, ignore_errors=True)

    def runtime_instance_id(self) -> str:
        """本次 Runtime 进程的实例 ID。每次 start() 分配新值。"""
        return self._runtime_instance_id or ""

    # ---------- Runtime 恢复（Phase 1C）----------

    async def export_runtime_state(self) -> RuntimeStateSnapshot | None:
        """从当前 Pi 进程导出可移植快照。"""
        entries = await self.get_entries()
        if not entries:
            return RuntimeStateSnapshot(
                conversation_id="unknown",
                entries=[],
                leaf_entry_id=None,
                fingerprint=[],
                source_runtime_instance_id=self.runtime_instance_id(),
            )
        # 计算指纹
        entry_ids = [str(e.get("id", "")) for e in entries]
        id_to_position = {eid: i for i, eid in enumerate(entry_ids)}
        fingerprints = []
        for i, entry in enumerate(entries):
            fp = fingerprint_from_entry(entry, branch_position=i)
            parent_id = entry.get("parentId")
            if parent_id and str(parent_id) in id_to_position:
                fp = EntryFingerprint(
                    entry_type=fp.entry_type,
                    role=fp.role,
                    content_hash=fp.content_hash,
                    parent_position=id_to_position[str(parent_id)],
                    branch_position=fp.branch_position,
                )
            fingerprints.append(fp)

        return RuntimeStateSnapshot(
            conversation_id="exported",
            entries=entries,
            leaf_entry_id=entry_ids[-1] if entry_ids else None,
            fingerprint=fingerprints,
            source_runtime_instance_id=self.runtime_instance_id(),
        )

    async def restore_runtime_state(self, snapshot: Any) -> RuntimeRestoreResult:
        """把快照物化为 Pi 会话文件并 ``switch_session``。

        **不触发任何 Provider 请求。**

        会话文件在切换**成功后必须保留**：``switch_session`` 让 Pi 进入持久化模式，
        之后每条新 entry 都追加到这个文件，``fork`` 也会重新读它。提前删掉，
        Pi 追加时会重建一个没有 session 头的文件，下一次分叉就报
        "Session file is not a valid pi session"。文件随适配器关闭统一清理。
        """
        if not isinstance(snapshot, RuntimeStateSnapshot) or not snapshot.entries:
            return RuntimeRestoreResult(
                success=False,
                failure_reason=RestoreFailureReason.NO_SNAPSHOT,
                error="快照为空或类型不正确",
            )
        try:
            restore_file = await asyncio.to_thread(
                _materialize_restore_file,
                self._session_dir,
                self._spec.cwd or "",
                snapshot.entries,
            )
        except Exception as exc:
            return RuntimeRestoreResult(
                success=False,
                failure_reason=RestoreFailureReason.MATERIALIZATION_FAILED,
                error=redact_text(str(exc)),
            )

        switched = False
        try:
            response = await self._rpc.request(
                {"type": "switch_session", "sessionPath": str(restore_file)},
                timeout=30,
            )
            if (response.get("success") is not True
                    or (response.get("data") or {}).get("cancelled") is not False):
                return RuntimeRestoreResult(
                    success=False,
                    failure_reason=RestoreFailureReason.SESSION_SWITCH_FAILED,
                    error=str(response.get("error", "switch_session 失败或被取消")),
                )
            switched = True
        except Exception as exc:
            return RuntimeRestoreResult(
                success=False,
                failure_reason=RestoreFailureReason.SESSION_SWITCH_FAILED,
                error=redact_text(str(exc)),
            )
        finally:
            # 只清理没被 Pi 接管的文件
            if not switched:
                with contextlib.suppress(OSError):
                    restore_file.unlink(missing_ok=True)

        try:
            validation = await self.validate_runtime_state(snapshot)
            if not validation.valid:
                return RuntimeRestoreResult(
                    success=False,
                    failure_reason=RestoreFailureReason.ENTRY_VALIDATION_FAILED,
                    error="恢复后的会话条目与目标快照不一致",
                )
            return RuntimeRestoreResult(
                success=True,
                restored_entry_count=len(snapshot.entries),
                error=None,
            )
        except Exception as exc:
            return RuntimeRestoreResult(
                success=False,
                failure_reason=RestoreFailureReason.SESSION_SWITCH_FAILED,
                error=redact_text(str(exc)),
            )

    async def validate_runtime_state(self, expected: Any) -> RuntimeValidationResult:
        """恢复后重新读 entries，与期望快照做语义校验。"""
        if not isinstance(expected, RuntimeStateSnapshot):
            return RuntimeValidationResult(valid=False, error="期望快照类型不正确")
        entries = await self.get_entries()
        return await asyncio.to_thread(_validate_entries, expected, entries)

    # ---------- 消息 ----------

    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        command: dict[str, Any] = {"type": "prompt", "message": text}
        if images:
            command["images"] = images
        response = await self._rpc.request(command)
        if response.get("success") is not True:
            raise RuntimeError(f"prompt 被拒绝：{response.get('error', 'unknown')}")

    async def new_session(self) -> None:
        response = await self._rpc.request({"type": "new_session"}, timeout=30)
        data = response.get("data") or {}
        if response.get("success") is not True or data.get("cancelled") is not False:
            raise RuntimeError("Pi 未确认新建空会话，不能继续发送")
        state = await self.get_state()
        entries = await self.get_entries()
        if (state.message_count or state.pending_message_count
                or state.is_streaming or state.is_compacting) or any(
            entry.get("type") in {"message", "custom_message", "compaction", "branch_summary"}
            for entry in entries
        ):
            raise RuntimeError("Pi 新会话仍含历史上下文，不能继续发送")
        self._memory_run_id = None

    def create_isolated(self) -> AgentKernel:
        """复制启动规格，创建不共享当前 Pi 会话树的临时实例。

        临时实例移除应用工具 IPC 凭据：标题生成之类的后台调用只需要模型文本能力，
        不应获得文件、网络或命令工具。stderr handler 也不复用，避免把临时实例的
        provider 观测误送进主会话协调器。
        """
        env = dict(self._spec.env or {})
        env.pop("LIMBOWAVE_TOOL_IPC", None)
        spec = SpawnSpec(
            argv=list(self._spec.argv),
            cwd=self._spec.cwd,
            env=env,
            stderr_handler=None,
        )
        return PiKernelAdapter(spec, capabilities=self._capabilities)

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
            provider=model.get("provider") if model else None,
            thinking_level=str(data.get("thinkingLevel", "off")),
            is_streaming=bool(data.get("isStreaming", False)),
            is_compacting=bool(data.get("isCompacting", False)),
            session_id=str(data.get("sessionId", "")),
            session_name=data.get("sessionName"),
            message_count=int(data.get("messageCount", 0)),
            pending_message_count=int(data.get("pendingMessageCount", 0)),
            raw=data,
        )

    async def get_context_usage(self) -> ContextUsage | None:
        """上下文占用估算（``get_session_stats.contextUsage``，合同 §三.7 已确认）。

        **这是估算**：token 计数来自 Pi/站点的估算策略，不是精确值。
        """
        response = await self._rpc.request({"type": "get_session_stats"})
        data = response.get("data") or {}
        usage = data.get("contextUsage")
        if not isinstance(usage, dict):
            return None
        return ContextUsage(
            tokens=int(usage.get("tokens", 0)),
            context_window=int(usage.get("contextWindow", 0)),
            percent=float(usage.get("percent", 0.0)),
        )

    async def set_model(self, provider: str, model_id: str) -> None:
        response = await self._rpc.request(
            {"type": "set_model", "provider": provider, "modelId": model_id}
        )
        if response.get("success") is not True:
            raise RuntimeError(f"set_model 失败：{response.get('error', 'unknown')}")

    async def set_memory_context(self, context: dict[str, object]) -> None:
        self._memory_run_id = str(context["run_id"])
        self._memory_ack = asyncio.get_running_loop().create_future()
        payload = json.dumps({"token": self._control_token, "context": context}, ensure_ascii=False)
        try:
            response = await self._rpc.request(
                {"type": "prompt", "message": f"/limbowave-memory {payload}"}
            )
            if response.get("success") is not True:
                raise RuntimeError("记忆上下文同步失败")
            await asyncio.wait_for(self._memory_ack, timeout=10)
        finally:
            self._memory_ack = None
            self._memory_run_id = None

    async def reload_models(
        self, env: dict[str, str], expected: tuple[tuple[str, str], ...]
    ) -> None:
        """经政策扩展的内部命令热更新模型目录。

        载荷走 RPC stdin（与启动时的环境变量一样只在父子进程之间），不落盘、
        不进日志。扩展命令执行失败时 Pi 仍回 success，所以**以可用模型清单为准**核对。
        """
        payload = json.dumps({"token": self._control_token, "env": env}, ensure_ascii=False)
        response = await self._rpc.request(
            {"type": "prompt", "message": f"/{_RELOAD_COMMAND} {payload}"}
        )
        if response.get("success") is not True:
            raise RuntimeError(f"模型目录热更新被拒绝：{response.get('error', 'unknown')}")
        listing = await self._rpc.request({"type": "get_available_models"})
        models = (listing.get("data") or {}).get("models") or []
        available = {(str(m.get("provider")), str(m.get("id"))) for m in models}
        missing = [f"{p}/{m}" for p, m in expected if (p, m) not in available]
        if missing:
            raise RuntimeError(f"模型目录热更新后仍不可用：{', '.join(missing)}")
        # 临时隔离内核（如会话命名）会复制启动环境；同步它，确保运行中新增的
        # 凭据也能被后续隔离实例解析。空值语义是删除变量。
        spawn_env = dict(self._spec.env or {})
        for name, value in env.items():
            if value:
                spawn_env[name] = value
            else:
                spawn_env.pop(name, None)
        self._spec.env = spawn_env

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
        raw_type = str(raw.get("type"))

        # 进程退出：只有**非预期**退出才算崩溃。优雅关闭（stdin EOF / 主动 kill）
        # 也会走到这里，必须区分，否则正常退出会把已完成的一轮误标为 interrupted。
        if raw_type == "process_exit":
            # 进程已不在：它物化/派生的会话文件不再有人读写
            self._discard_session_files()
            if raw.get("expected"):
                return
            self._publish(
                KernelEvent(
                    kind="runtime.exited",
                    payload={"returncode": raw.get("returncode"), "reason": "Pi 进程异常退出"},
                )
            )
            return

        kind = _EVENT_MAP.get(raw_type, f"raw.{raw_type or 'unknown'}")
        # 扩展 UI 请求需要应答——异步裁决，不阻塞事件泵
        if kind == "ui.request":
            self._schedule_ui_answer(raw)
        self._publish(KernelEvent(kind=kind, payload=raw))

    def _publish(self, event: KernelEvent) -> None:
        # 事件进入队列（供异步迭代）与同步回调（供订阅）双通道
        self._event_queue.put_nowait(event)
        for handler in list(self._handlers):
            # 订阅者异常不得拖垮事件泵
            try:
                handler(event)
            except Exception:
                _LOG.exception("pi.subscriber_failed", extra={"event_kind": event.kind})

    def _on_observation(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind in {"provider.request", "provider.response", "tool_call.denied", "memory.ready"}:
            level = logging.WARNING if kind == "tool_call.denied" else logging.DEBUG
            status = payload.get("status")
            if isinstance(status, int) and status >= 400:
                level = logging.WARNING
            _LOG.log(level, "pi.observation", extra={
                "observation_kind": kind,
                **{key: payload[key] for key in (
                    "status", "run_id", "toolName", "provider", "model", "endpoint_id",
                ) if isinstance(payload.get(key), (str, int, bool))},
            })
        if payload.get("kind") == "memory.ready":
            if (payload.get("run_id") == self._memory_run_id
                    and self._memory_ack is not None and not self._memory_ack.done()):
                self._memory_ack.set_result(None)
            return
        if self._observation_handler is not None:
            try:
                self._observation_handler(payload)
            except Exception:
                _LOG.exception("pi.observation_handler_failed", extra={"observation_kind": kind})

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
                KernelCapability.RUNTIME_RESTORE,
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
