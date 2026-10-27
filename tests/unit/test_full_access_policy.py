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
