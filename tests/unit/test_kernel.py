"""AgentKernel 抽象与能力协商的单元测试（不启动真实进程）。"""

from __future__ import annotations

from limbowave.application.kernel import KernelCapabilities, KernelCapability
from limbowave.infrastructure.pi_adapter import _default_capabilities


def test_capability_membership() -> None:
    caps = KernelCapabilities(
        capabilities=frozenset(
            {KernelCapability.BRANCHING, KernelCapability.ABORT_PRESERVES_PARTIAL}
        )
    )
    assert caps.has(KernelCapability.BRANCHING)
    assert not caps.has(KernelCapability.FINAL_REQUEST_HOOK)


def test_capability_missing() -> None:
    caps = KernelCapabilities(capabilities=frozenset({KernelCapability.BRANCHING}))
    required = {KernelCapability.BRANCHING, KernelCapability.CUSTOM_COMPACTION}
    assert caps.missing(required) == {KernelCapability.CUSTOM_COMPACTION}


def test_default_pi_capabilities_match_gate_results() -> None:
    """默认能力集必须与 P0 闸门实机结论一致（合同 §十一）。persistent_session 不在其中。"""
    caps = _default_capabilities()
    expected = {
        KernelCapability.IN_MEMORY_SESSION,
        KernelCapability.BRANCHING,
        KernelCapability.ABORT_PRESERVES_PARTIAL,
        KernelCapability.FINAL_REQUEST_HOOK,
        KernelCapability.CUSTOM_COMPACTION,
        KernelCapability.TOOL_PREFLIGHT_HOOK,
        KernelCapability.INTERNAL_RETRY_DISABLE,
        KernelCapability.TELEMETRY_DISABLE,
    }
    assert caps.capabilities == expected
    # 关键：按裁决 3 用 --no-session，不得声称持久化能力
    assert not caps.has(KernelCapability.PERSISTENT_SESSION)
