import json

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.permissions import Decision, PermissionPreset
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory


@pytest.mark.parametrize("preset", [PermissionPreset.READ_ONLY, PermissionPreset.CUSTOM])
@pytest.mark.parametrize("answer", ["once", "deny"])
async def test_create_reminder_requires_explicit_approval(tmp_path, monkeypatch, preset, answer):
    permissions = PermissionService(in_memory_uow_factory())
    gateway = ToolGateway(permissions, tmp_path)
    prompts = []
    async def ask(window, request, reason, scope):
        prompts.append(request.raw_params)
        assert scope is None  # No misleading permanent grant option.
        return answer
    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(
        None, permissions, lambda: "c", gateway=gateway,
        preset_provider=lambda _, preset=preset: preset,
    )
    params = {"reason": "喝水", "delay_seconds": 60}
    result = await handler("limbowave.gate:set_reminder", json.dumps({
        "tool": "set_reminder", "input": params,
    }))
    assert result is (answer == "once")
    assert prompts == [params]
    # The prompt and answer can share a timestamp; locate the final decision explicitly.
    audits = permissions.list_audit("c")
    expected = Decision.ALLOW if result else Decision.DENY
    final = [audit for audit in audits if audit.decision is expected]
    assert len(final) == 1
    assert final[0].user_confirmed is result
    assert final[0].params == params


async def test_management_no_prompt_and_chat_only_still_blocks(tmp_path, monkeypatch):
    permissions = PermissionService(in_memory_uow_factory())
    gateway = ToolGateway(permissions, tmp_path)
    async def unexpected(*args):
        raise AssertionError("No reminder management prompt expected")
    monkeypatch.setattr("limbowave.app._ask_permission", unexpected)
    for preset in [PermissionPreset.CHAT_ONLY, PermissionPreset.READ_ONLY]:
        handler = _make_permission_handler(
            None, permissions, lambda: "c", gateway=gateway,
            preset_provider=lambda _, preset=preset: preset,
        )
        for tool, params in [("list_reminders", {}), ("cancel_reminder", {"reminder_id": "id"})]:
            assert await handler(f"limbowave.gate:{tool}", json.dumps({
                "tool": tool, "input": params,
            })) is (preset is PermissionPreset.READ_ONLY)


async def test_confirmation_expiring_on_conversation_switch_is_denied(tmp_path, monkeypatch):
    permissions = PermissionService(in_memory_uow_factory())
    gateway = ToolGateway(permissions, tmp_path)
    current = "c"
    async def ask(*args):
        nonlocal current
        current = "other"
        return "once"
    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(
        None, permissions, lambda: current, gateway=gateway,
        preset_provider=lambda _: PermissionPreset.READ_ONLY,
    )
    assert not await handler("limbowave.gate:set_reminder", json.dumps({
        "tool": "set_reminder", "input": {"reason": "x", "delay_seconds": 30},
    }))
