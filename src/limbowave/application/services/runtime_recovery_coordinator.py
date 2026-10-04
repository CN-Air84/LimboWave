"""Runtime 恢复协调器：Runtime 丢失后从应用镜像恢复运行上下文。

职责边界（Phase 1C）：

- ``RunCoordinator``：一轮消息从 running 到终态的编排。
- ``RuntimeRecoveryCoordinator``：Runtime 丢失、重启、恢复和验证（本文件）。
- ``SessionController``：GUI 命令与展示事件 facade。
- ``PiKernelAdapter``：Pi 会话物化、切换与协议实现。

**恢复协调器不得自动调用 ``send_message()``。** 恢复完成只是让会话重新可用。

状态机：

    available → lost → restarting → restoring → validating → recovered / recovery_failed

调用方（app.py）负责：
1. 检测 Runtime 退出后调用 ``on_runtime_exited()``
2. 从 RunCoordinator 的仓库构建 ``RuntimeStateSnapshot``
3. 用 ``build_kernel()`` 创建替代内核并 ``attach_replacement_kernel()``
4. 调用 ``await recover(snapshot)``
5. 成功后调用 ``make_available()``

关键约束：

- interrupted Run 保持原状态，恢复后不得改成 completed。
- 不自动重发导致中断的用户消息。
- 多次收到 Runtime 退出事件，只启动一次恢复流程（幂等）。
- 旧进程的迟到事件（通过 runtime_instance_id 区分）被拒收。
- 恢复成功前，不把状态栏改回"就绪"。
- 用户主动关闭应用不触发恢复。
- 只使用最后一次成功提交的稳定镜像，崩溃时仍在暂存区的数据不作为恢复源。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from limbowave.application.events import (
    RUN_INTERRUPTED,
    ChatEvent,
    ChatEventHandler,
)
from limbowave.application.kernel import AgentKernel
from limbowave.domain.redaction import redact_text
from limbowave.domain.runtime_state import (
    RecoveryState,
    RestoreFailureReason,
    RuntimeStateSnapshot,
    RuntimeValidationResult,
)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """一次恢复尝试的完整结果（供 UI 与测试读取）。"""

    success: bool
    state: RecoveryState
    restored_entry_count: int = 0
    validation: RuntimeValidationResult | None = None
    failure_reason: str | None = None
    error: str | None = None
    from_mirror_entry_count: int = 0


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RuntimeRecoveryCoordinator:
    """管理 Runtime 丢失后的恢复生命周期。

    本协调器**不拥有**内核——它接收替代内核（由调用方创建），
    并通过 ``AgentKernel`` 的恢复能力接口完成物化、切换与校验。
    """

    def __init__(
        self,
        *,
        clock: Any = None,
    ) -> None:
        self._phase = RecoveryState.AVAILABLE
        self._runtime_instance_id: str = ""
        self._recovery_generation = 0
        self._handlers: list[ChatEventHandler] = []
        self._kernel: AgentKernel | None = None

    # ---------- 只读状态 ----------

    @property
    def phase(self) -> RecoveryState:
        return self._phase

    @property
    def recovery_generation(self) -> int:
        return self._recovery_generation

    @property
    def can_send(self) -> bool:
        """恢复期间禁止发送：明确拒绝而非隐式排队。"""
        return self._phase == RecoveryState.AVAILABLE

    # ---------- 订阅 ----------

    def subscribe(self, handler: ChatEventHandler) -> Any:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe

    def _emit(self, kind: str, **data: Any) -> None:
        event = ChatEvent(kind=kind, data=data)
        for handler in list(self._handlers):
            # 订阅者异常不得拖垮恢复流程
            with contextlib.suppress(Exception):
                handler(event)

    # ---------- Runtime 退出 ----------

    def on_runtime_exited(
        self,
        *,
        instance_id: str = "",
        expected: bool = False,
    ) -> bool:
        """Runtime 退出通知。返回 True 表示启动了恢复流程。

        - 优雅关闭（``expected=True``）不触发恢复。
        - 非预期退出且当前状态为 available → 冻结发送、进入恢复。
        - 多次退出事件只启动一次（幂等）。
        """
        if expected:
            # 用户主动关闭：不恢复，只保持不可用
            return False
        if self._phase != RecoveryState.AVAILABLE:
            # 已在恢复中或已恢复/失败：幂等跳过
            return False
        self._phase = RecoveryState.LOST
        self._recovery_generation += 1
        self._runtime_instance_id = instance_id
        self._emit(RUN_INTERRUPTED, message="Runtime 异常退出，正在恢复…")
        return True

    def attach_replacement_kernel(self, kernel: AgentKernel) -> None:
        """注入一个新启动的替代内核。协调器接管后续恢复。"""
        self._kernel = kernel
        self._phase = RecoveryState.RESTARTING

    # ---------- 恢复 ----------

    async def recover(
        self,
        snapshot: RuntimeStateSnapshot | None,
    ) -> RecoveryResult:
        """执行恢复流程。

        ``snapshot`` 由调用方从仓库构建（只读已 commit 的数据）。
        传 None 或空快照表示无可恢复状态。
        """
        kernel = self._kernel
        if kernel is None:
            self._phase = RecoveryState.RECOVERY_FAILED
            return RecoveryResult(
                success=False,
                state=RecoveryState.RECOVERY_FAILED,
                failure_reason=RestoreFailureReason.NO_SNAPSHOT.value,
                error="没有可用的替代内核",
            )

        if snapshot is None or not snapshot.entries:
            self._phase = RecoveryState.RECOVERY_FAILED
            return RecoveryResult(
                success=False,
                state=RecoveryState.RECOVERY_FAILED,
                failure_reason=RestoreFailureReason.NO_SNAPSHOT.value,
                error="无可恢复状态",
                from_mirror_entry_count=0,
            )

        self._phase = RecoveryState.RESTORING

        # 恢复：物化 → switch_session → 重新读 entries
        restore_result = await kernel.restore_runtime_state(snapshot)
        if not restore_result.success:
            reason = (
                restore_result.failure_reason.value if restore_result.failure_reason else "unknown"
            )
            self._phase = RecoveryState.RECOVERY_FAILED
            return RecoveryResult(
                success=False,
                state=RecoveryState.RECOVERY_FAILED,
                failure_reason=reason,
                error=redact_text(str(restore_result.error or "")),
                restored_entry_count=restore_result.restored_entry_count,
                from_mirror_entry_count=len(snapshot.entries),
            )

        # 校验：重新读 entries，与期望快照做语义比对
        self._phase = RecoveryState.RESTORING
        validation = await kernel.validate_runtime_state(snapshot)
        if not validation.valid:
            self._phase = RecoveryState.RECOVERY_FAILED
            return RecoveryResult(
                success=False,
                state=RecoveryState.RECOVERY_FAILED,
                failure_reason=RestoreFailureReason.ENTRY_VALIDATION_FAILED.value,
                error="恢复后语义校验不一致",
                restored_entry_count=restore_result.restored_entry_count,
                validation=validation,
                from_mirror_entry_count=len(snapshot.entries),
            )

        # 成功
        self._phase = RecoveryState.RECOVERED
        # 更新到新内核的实例 ID，使后续 stale 检查用新 ID
        if kernel.runtime_instance_id():
            self._runtime_instance_id = kernel.runtime_instance_id()
        return RecoveryResult(
            success=True,
            state=RecoveryState.RECOVERED,
            restored_entry_count=restore_result.restored_entry_count,
            validation=validation,
            from_mirror_entry_count=len(snapshot.entries),
        )

    def make_available(self) -> None:
        """恢复成功后由调用方调用：开放发送。**不会自动重发。**"""
        if self._phase == RecoveryState.RECOVERED:
            self._phase = RecoveryState.AVAILABLE
            self._kernel = None

    def reject_stale_event(self, event_instance_id: str) -> bool:
        """判断事件是否来自已废弃的旧 Runtime。

        返回 True 表示该事件应被拒收（旧实例的迟到事件）。
        """
        if not self._runtime_instance_id:
            return False
        return event_instance_id != self._runtime_instance_id
