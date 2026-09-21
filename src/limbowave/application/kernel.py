"""AgentKernel：应用与 Agent 执行引擎之间的稳定接口。

设计依据：docs/architecture/adr-0001-agent-kernel.md。

关键约束：
- 应用**不直接**依赖 Pi 的具体 RPC 命令；只依赖本接口。
- Pi 只是 ``AgentKernel`` 的第一个实现（``PiKernelAdapter``）。
- 能力不是恒定的：启动时由适配器返回 ``KernelCapabilities``，应用据此决定能用哪些功能。
  缺失硬要求能力时，适配器应在 ``capabilities()`` 中如实报告，由应用决定是否拒绝启动。

线程模型：实现内部异步。本接口暴露为 async，由 qasync 在 GUI 侧驱动。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class KernelCapability(enum.StrEnum):
    """内核能力协商。应用不得假定所有能力都存在。"""

    PERSISTENT_SESSION = "persistent_session"
    IN_MEMORY_SESSION = "in_memory_session"
    BRANCHING = "branching"
    ABORT_PRESERVES_PARTIAL = "abort_preserves_partial"
    FINAL_REQUEST_HOOK = "final_request_hook"
    CUSTOM_COMPACTION = "custom_compaction"
    TOOL_PREFLIGHT_HOOK = "tool_preflight_hook"
    INTERNAL_RETRY_DISABLE = "internal_retry_disable"
    TELEMETRY_DISABLE = "telemetry_disable"


@dataclass(frozen=True, slots=True)
class KernelCapabilities:
    """一组内核能力。``frozenset`` 便于集合运算。"""

    capabilities: frozenset[KernelCapability] = field(default_factory=frozenset)

    def has(self, capability: KernelCapability) -> bool:
        return capability in self.capabilities

    def missing(self, required: set[KernelCapability]) -> set[KernelCapability]:
        return required - self.capabilities


@dataclass(frozen=True, slots=True)
class KernelEvent:
    """归一化的运行时事件。``kind`` 为抽象层事件名，``payload`` 为原始数据。

    适配器负责把 Pi 的具体事件（如 ``message_update``、``tool_execution_start``）
    归一到这里的抽象 ``kind``。应用层只认识这些抽象名字。
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KernelState:
    """内核会话状态快照（对应 Pi 的 ``get_state`` 响应，但字段归一）。"""

    model_id: str | None
    thinking_level: str
    is_streaming: bool
    is_compacting: bool
    session_id: str
    session_name: str | None
    message_count: int
    pending_message_count: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """一次压缩的结果。"""

    summary: str
    tokens_before: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    """最终传输快照（由 final-request 钩子供数）。

    对应 ADR 裁决 1 的「最终传输快照」：最终 URL、请求头（敏感值应已脱敏）、请求体。
    """

    url: str | None
    headers: dict[str, str]
    payload: dict[str, Any]
    status: int | None = None


# 事件订阅回调类型
EventHandler = Callable[[KernelEvent], None]

# 权限裁决回调：收到 (标题, 详情)，返回 True=允许 / False=拒绝。
# 本阶段抽象层只承载 confirm 语义；结构化资源授权属后续阶段。
PermissionHandler = Callable[[str, str], Awaitable[bool]]


class AgentKernel(ABC):
    """应用与 Agent 执行引擎之间的抽象接口。

    应用层（用例编排）只依赖这个接口。``PiKernelAdapter`` 是它对接 Pi 的实现。
    """

    @abstractmethod
    def capabilities(self) -> KernelCapabilities:
        """返回该实现支持的能力集。启动时调用一次。"""

    @abstractmethod
    async def start(self) -> None:
        """启动内核（拉起运行时进程/连接）。幂等。"""

    @abstractmethod
    async def shutdown(self) -> None:
        """优雅关闭。对 Pi 即关闭 stdin（EOF → 退出码 0）。"""

    @abstractmethod
    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        """发送一条用户消息。仅负责投递；流式结果经事件回调送达。"""

    @abstractmethod
    async def abort(self) -> None:
        """中止当前操作，等待内核进入 idle。"""

    @abstractmethod
    async def get_state(self) -> KernelState:
        """返回当前会话状态快照。"""

    @abstractmethod
    async def set_model(self, provider: str, model_id: str) -> None:
        """切换当前模型（应用侧路由决策的执行点）。"""

    @abstractmethod
    async def set_thinking_level(self, level: str) -> None:
        """设置思考强度。"""

    @abstractmethod
    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """返回会话树的条目（应用权威镜像的数据源）。"""

    @abstractmethod
    async def fork(self, entry_id: str) -> str:
        """从某条用户消息分叉，返回被分叉的原文。"""

    @abstractmethod
    async def compact(self, custom_instructions: str | None = None) -> CompactionResult:
        """触发压缩。配合 custom_compaction 能力实现应用侧接管。"""

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """订阅运行时事件。返回退订函数。"""

    @abstractmethod
    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """设置权限裁决回调。未设置时实现必须默认拒绝（合同 §十一 GATE-04）。"""

    @abstractmethod
    def events(self) -> AsyncIterator[KernelEvent]:
        """以异步迭代器方式消费事件（供 GUI 侧驱动）。"""
