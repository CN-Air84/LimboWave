"""扩展 ↔ 应用的结构化裁决协议（Phase 6）。

扩展发 `ctx.ui.confirm("limbowave.gate:<工具>", <JSON 载荷>)`；
应用解析后跑权限策略引擎。这条协议是本阶段新增的**双边契约**——
两侧任何一侧改了格式，这里必须失败。

不测 Qt 菜单本身，只测解析与档位分流：仅聊天、自由读取、完全访问和自定义。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from limbowave.app import _make_permission_handler, _parse_gate_request, _paths_from_input
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.permissions import Capability, PermissionPreset
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


def test_parses_structured_payload() -> None:
    payload = {"tool": "read", "input": {"path": "notes.md"}}
    parsed = _parse_gate_request("limbowave.gate:read", json.dumps(payload))
    assert parsed is not None
    assert parsed["tool"] == "read"
    assert parsed["input"]["path"] == "notes.md"


def test_parses_user_bash_payload() -> None:
    payload = {"tool": "user_bash", "input": {"command": "ls"}, "command": "ls"}
    parsed = _parse_gate_request("limbowave.gate:user_bash", json.dumps(payload))
    assert parsed is not None
    assert parsed["command"] == "ls"


def test_rejects_non_gate_title() -> None:
    """没有前缀的标题不是我们的协议——返回 None，走旧的直接询问路径。"""
    assert _parse_gate_request("允许执行工具 X？", "{}") is None


def test_rejects_malformed_json() -> None:
    assert _parse_gate_request("limbowave.gate:read", "not json") is None


def test_rejects_payload_without_tool() -> None:
    assert _parse_gate_request("limbowave.gate:read", json.dumps({"input": {}})) is None


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"path": "a.txt"}, ("a.txt",)),
        ({"file_path": "b/c.md"}, ("b/c.md",)),
        ({}, ()),
        ({"other": "x"}, ()),
    ],
)
def test_paths_from_input(params: dict, expected: tuple[str, ...]) -> None:
    """工具参数里的路径被抽出来做资源范围判定。"""
    assert _paths_from_input(params) == expected


# ---------- 裁决分流（权限档位由输入区预先选择） ----------

CONV = "c1"


@pytest.fixture
def gate_env(tmp_path: Path) -> tuple[PermissionService, ToolGateway]:
    permissions = PermissionService(in_memory_uow_factory(InMemoryStore()))
    return permissions, ToolGateway(permissions, tmp_path)


def _gate(tool: str, params: dict[str, object]) -> tuple[str, str]:
    """按扩展的协议格式拼一次模型工具调用的裁决请求。"""
    return f"limbowave.gate:{tool}", json.dumps({"tool": tool, "input": params})


def _bash(command: str) -> tuple[str, str]:
    payload = {"tool": "user_bash", "input": {"command": command}, "command": command}
    return "limbowave.gate:user_bash", json.dumps(payload)


def _handler(
    gate_env: tuple[PermissionService, ToolGateway],
    preset: PermissionPreset,
) -> Callable[[str, str], Awaitable[bool]]:
    permissions, gateway = gate_env
    return _make_permission_handler(
        None,  # type: ignore[arg-type]
        permissions,
        lambda: CONV,
        gateway=gateway,
        preset_provider=lambda _conversation_id: preset,
    )


async def test_chat_only_blocks_tools_and_audits(
    gate_env: tuple[PermissionService, ToolGateway],
) -> None:
    permissions, _gateway = gate_env
    handler = _handler(gate_env, PermissionPreset.CHAT_ONLY)
    assert await handler(*_gate("list_directory", {})) is False
    assert permissions.list_grants(CONV) == []
    audit = permissions.list_audit(CONV)
    assert audit and not any(a.user_confirmed for a in audit)


async def test_read_only_allows_normal_reads_but_not_writes_or_sensitive_paths(
    gate_env: tuple[PermissionService, ToolGateway],
) -> None:
    handler = _handler(gate_env, PermissionPreset.READ_ONLY)
    assert await handler(*_gate("list_directory", {"path": ""})) is True
    assert await handler(*_gate("create_file", {"path": "n.txt", "content": "x"})) is False
    assert await handler(*_gate("stat_file", {"path": ".ssh/id_rsa"})) is False


async def test_full_access_allows_tools_without_runtime_prompt(
    gate_env: tuple[PermissionService, ToolGateway],
) -> None:
    handler = _handler(gate_env, PermissionPreset.FULL_ACCESS)
    assert await handler(*_gate("create_file", {"path": "n.txt", "content": "x"})) is True
    assert await handler(*_bash("Get-Date")) is True
    assert await handler(*_bash("rm -rf C:/tmp/x")) is True


async def test_custom_uses_saved_capability_grants(
    gate_env: tuple[PermissionService, ToolGateway],
) -> None:
    permissions, gateway = gate_env
    permissions.replace_custom_grants(
        CONV,
        {Capability.FILE_READ},
        workspace_root=str(gateway.workspace_root),
    )
    handler = _handler(gate_env, PermissionPreset.CUSTOM)
    assert await handler(*_gate("list_directory", {})) is True
    assert await handler(*_gate("create_file", {"path": "n.txt", "content": "x"})) is False


async def test_invalid_path_is_rejected_without_prompt(
    gate_env: tuple[PermissionService, ToolGateway],
) -> None:
    handler = _handler(gate_env, PermissionPreset.FULL_ACCESS)
    assert await handler(*_gate("stat_file", {"path": "../outside.txt"})) is False


# ---------- 会话级站点覆盖与视觉能力（Phase 10 / Task 4.4 回归） ----------


def test_run_context_uses_override() -> None:
    """覆盖生效时，意图快照记录**实际使用的端点**（记录与执行不能脱节）。"""
    from limbowave.app import _run_context
    from limbowave.application.kernel import KernelSetup

    class _Kernel:
        pass

    setup = KernelSetup(
        kernel=_Kernel(),  # type: ignore[arg-type]
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat"},
        supports_images=True,
    )
    override: dict[str, str | None] = {"endpoint": None}
    context = _run_context(setup, override)

    base = context()
    assert base.endpoint_id == "relay-a"
    assert "默认绑定" in base.routing_reason
    assert base.supports_images is True  # 视觉能力透传（否则图片会被永远拒绝）

    override["endpoint"] = "relay-b"
    switched = context()
    assert switched.endpoint_id == "relay-b"
    assert "覆盖" in switched.routing_reason
    assert "relay-a" in switched.routing_reason  # 说明配置默认是什么

    override["endpoint"] = None
    assert context().endpoint_id == "relay-a"  # 恢复默认


def test_run_context_without_setup() -> None:
    from limbowave.app import _run_context

    context = _run_context(None)
    assert context().endpoint_id == "unknown"
    assert context().supports_images is False


def test_run_context_tracks_logical_model_switch() -> None:
    """输入框切换逻辑模型后，后续请求快照必须读取新的路由上下文。"""
    from limbowave.app import _run_context
    from limbowave.application.kernel import KernelSetup

    class _Kernel:
        pass

    first = KernelSetup(
        kernel=_Kernel(),  # type: ignore[arg-type]
        logical_model_id="model-a",
        endpoint_id="relay-a",
        routing_reason="模型 A 默认绑定",
    )
    second = KernelSetup(
        kernel=first.kernel,
        logical_model_id="model-b",
        endpoint_id="relay-b",
        routing_reason="模型 B 默认绑定",
    )
    state: dict[str, KernelSetup | None] = {"value": first}
    context = _run_context(state)

    assert context().logical_model_id == "model-a"
    state["value"] = second
    assert context().logical_model_id == "model-b"
    assert context().endpoint_id == "relay-b"
