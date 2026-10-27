"""Full access is a persisted conversation rule, not a model-supplied flag."""

from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.domain.conversation import Conversation
from limbowave.domain.permissions import (
    Capability,
    Decision,
    PermissionPreset,
    ResourceScope,
    RiskLevel,
    ToolRequest,
)
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


@pytest.fixture
def permissions():
    factory = in_memory_uow_factory(InMemoryStore())
    with factory() as uow:
        for key, preset in (("full", PermissionPreset.FULL_ACCESS),
                            ("normal", PermissionPreset.READ_ONLY)):
            uow.conversations.add(Conversation(
                id=key, title=key, created_at=datetime.now(UTC), permission_preset=preset,
            ))
        uow.commit()
    return PermissionService(factory)


@pytest.mark.parametrize("capability", list(Capability))
def test_full_access_authorizes_every_capability_and_audits(permissions, capability):
    request = ToolRequest("test_tool", capability)
    result = permissions.authorize(request, "full")
    assert result.decision is Decision.ALLOW
    assert result.matched_rule == "preset.full_access"
    audit = permissions.list_audit("full")[-1]
    assert audit.decision is Decision.ALLOW
    assert not audit.user_confirmed  # A preset is not a fabricated per-call confirmation.
    assert audit.matched_rule == "preset.full_access"


def test_full_access_preserves_risk_and_explicit_denial(permissions):
    request = ToolRequest("run_command", Capability.TERMINAL,
                          ResourceScope(command="rm -rf /"))
    assert permissions.authorize(request, "full").risk is RiskLevel.HIGH
    result = permissions.authorize(request, "full", user_confirmed=True, user_denied=True)
    assert result.decision is Decision.DENY
    assert not permissions.list_audit("full")[-1].user_confirmed


@pytest.mark.parametrize("conversation", ["normal", "missing"])
def test_other_conversations_do_not_inherit_full_access(permissions, conversation):
    request = ToolRequest("run_command", Capability.TERMINAL)
    assert permissions.authorize(request, conversation).decision is Decision.CONFIRM
    assert permissions.set_preset("full", PermissionPreset.READ_ONLY)
    assert permissions.authorize(request, "full").decision is Decision.CONFIRM


@pytest.mark.parametrize("confirmed", [True, False])
def test_gateway_derives_network_scope_from_preset_not_confirmation(
    permissions, tmp_path, confirmed,
):
    from limbowave.application.services.tool_access import has_full_access
    from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
    from limbowave.domain.network import WebResponse
    from limbowave.infrastructure.web_access import validate_public_url

    seen = []

    def fetch(url):
        seen.append(has_full_access())
        normalized, _, _ = validate_public_url(url)
        return WebResponse(normalized, 200, "text/plain", b"hello", "2026-10-27T00:00:00Z")

    gateway = ToolGateway(permissions, tmp_path, network=NetworkTools(page_fetcher=fetch))
    params = {"url": "http://127.0.0.1:8080/"}
    assert gateway.invoke("read_url", params, "full", user_confirmed=confirmed).ok
    assert not has_full_access()
    assert not gateway.invoke("read_url", params, "normal", user_confirmed=True).ok
    assert seen == [True, False]
    assert not gateway.invoke("read_url", {**params, "full_access": True}, "normal").ok


def test_remote_origin_cannot_create_full_access_scope(permissions, tmp_path):
    from limbowave.application.services.tool_access import has_full_access
    from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway

    calls = []
    gateway = ToolGateway(permissions, tmp_path, origin_provider=lambda: "web",
                          network=NetworkTools(fetcher=lambda url: calls.append(url)))
    result = gateway.invoke("read_url", {"url": "http://localhost/"}, "full", user_confirmed=True)
    assert not result.ok and not calls and not has_full_access()


def test_full_access_file_scope_is_validated_and_never_leaks(permissions, tmp_path):
    from limbowave.application.services.tool_gateway import ToolGateway

    root = tmp_path / "workspace"
    root.mkdir()
    gateway = ToolGateway(permissions, root)
    outside = {"path": "../outside.txt", "content": "test data"}
    assert gateway.invoke("create_file", outside, "full").ok
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "test data"
    assert gateway.invoke("stat_file", {"path": "../outside.txt"}, "full").ok
    assert not gateway.invoke("stat_file", {"path": "../outside.txt"}, "normal",
                              user_confirmed=True).ok
    assert not gateway.invoke("stat_file", {"path": "bad\x00path"}, "full").ok
    audit = permissions.list_audit("full")[-1]
    assert audit.actual_paths == (str(tmp_path / "outside.txt"),)
