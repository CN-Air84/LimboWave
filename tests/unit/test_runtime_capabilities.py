"""CAP-01 snapshots reflect current services without running or authorizing tools."""

from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.reminder_service import ReminderService
from limbowave.application.services.run_origin import remote_origin
from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
from limbowave.domain.conversation import Conversation
from limbowave.domain.memory import MemoryPolicy, MemoryRunContext
from limbowave.domain.permissions import Capability, PermissionPreset
from limbowave.domain.reminder import ReminderContext
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from tests.unit.test_memory_service import memory_stack as memory_stack


@pytest.fixture
def stack(tmp_path):
    factory = in_memory_uow_factory()
    with factory() as uow:
        uow.conversations.add(
            Conversation(
                id="c",
                title="test",
                created_at=datetime.now(UTC),
            )
        )
        uow.commit()
    permissions = PermissionService(factory)
    return ToolGateway(permissions, tmp_path), permissions, factory


def test_unconfigured_is_not_inferred_from_static_tool_registration(stack):
    gateway, permissions, _ = stack
    snapshot = gateway.runtime_capabilities("c")
    assert snapshot["capabilities"]["web_search"]["status"] == "unconfigured"
    assert snapshot["capabilities"]["read_url"]["status"] == "unconfigured"
    assert snapshot["capabilities"]["reminders"]["status"] == "unconfigured"
    assert snapshot["capabilities"]["background_monitoring"]["status"] == "unconfigured"
    assert snapshot["capabilities"]["file_read"]["status"] == "active"
    assert not permissions.list_audit("c")


def test_url_fetcher_does_not_enable_search_or_probe_network(stack):
    gateway, _, _ = stack
    gateway._network = NetworkTools(fetcher=lambda _: pytest.fail("must not probe network"))
    capabilities = gateway.runtime_capabilities("c")["capabilities"]
    assert capabilities["read_url"]["status"] == "active"
    assert capabilities["web_search"]["status"] == "unconfigured"
    gateway._network = NetworkTools()
    assert gateway.runtime_capabilities("c")["capabilities"]["read_url"]["status"] == "unconfigured"


def test_reminders_are_local_run_bound_and_creation_needs_confirmation(stack):
    gateway, permissions, factory = stack
    gateway.reminder_service = ReminderService(factory)
    gateway.reminder_context = lambda: ReminderContext("c", "b", "m")
    item = gateway.runtime_capabilities("c")["capabilities"]["reminders"]
    assert item == {
        "status": "active",
        "scope": "local_only",
        "permission": "requires_confirmation",
    }
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    assert gateway.runtime_capabilities("c")["capabilities"]["reminders"]["permission"] == "allowed"
    gateway.reminder_context = lambda: ReminderContext("other", "b", "m")
    assert gateway.runtime_capabilities("c")["capabilities"]["reminders"]["status"] == "restricted"


def test_preset_changes_are_live_and_do_not_grant_permissions(stack):
    gateway, permissions, _ = stack
    assert gateway.runtime_capabilities("c")["capabilities"]["file_write"]["status"] == "restricted"
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    assert gateway.runtime_capabilities("c")["capabilities"]["file_write"]["status"] == "active"
    permissions.set_preset("c", PermissionPreset.CHAT_ONLY)
    assert gateway.runtime_capabilities("c")["capabilities"]["file_read"]["status"] == "restricted"
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    assert (
        gateway.runtime_capabilities("c")["capabilities"]["file_read"]["permission"]
        == "requires_confirmation"
    )
    grant = permissions.grant("c", Capability.FILE_READ, allowed_paths=("private-path",))
    snapshot = gateway.runtime_capabilities("c")
    assert snapshot["capabilities"]["file_read"]["permission"] == "scoped"
    assert "private-path" not in str(snapshot)
    permissions.revoke(grant.id)
    assert (
        gateway.runtime_capabilities("c")["capabilities"]["file_read"]["permission"]
        == "requires_confirmation"
    )
    assert not permissions.list_audit("c")


@pytest.mark.parametrize("network_grant", [False, True], ids=["no-grant", "network-grant"])
@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_custom_search_snapshot_matches_blocked_gate(stack, network_grant, configured):
    gateway, permissions, _ = stack
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    if network_grant:
        permissions.grant("c", Capability.NETWORK, allowed_domains=("*",))
    grants = permissions.list_grants("c")
    gateway._network = NetworkTools(
        fetcher=lambda _: pytest.fail("snapshot must not fetch a URL"),
        searcher=lambda _: pytest.fail("snapshot must not run a search"),
        search_ready=lambda: configured,
    )
    capabilities = gateway.runtime_capabilities("c")["capabilities"]
    assert capabilities["web_search"] == {
        "status": "restricted" if configured else "unconfigured",
        "permission": "denied",
    }
    # Only search is blocked: CUSTOM still describes URL grants independently.
    assert capabilities["read_url"] == {
        "status": "active",
        "permission": "scoped" if network_grant else "requires_confirmation",
    }
    for preset in (PermissionPreset.READ_ONLY, PermissionPreset.FULL_ACCESS):
        permissions.set_preset("c", preset)
        assert gateway.runtime_capabilities("c")["capabilities"]["web_search"] == {
            "status": "active" if configured else "unconfigured",
            "permission": "allowed" if configured else "denied",
        }
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    assert gateway.runtime_capabilities("c")["capabilities"]["web_search"] == capabilities[
        "web_search"
    ]
    assert permissions.list_grants("c") == grants
    assert not permissions.list_audit("c")


def test_remote_origin_never_advertises_local_tools(stack):
    gateway, permissions, _ = stack
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    with remote_origin():
        assert all(
            item["status"] == "restricted"
            for item in gateway.runtime_capabilities("c")["capabilities"].values()
        )
    gateway.origin_provider = lambda: "web"
    assert all(
        item["status"] == "restricted"
        for item in gateway.runtime_capabilities("c")["capabilities"].values()
    )


def test_memory_snapshot_respects_independent_policy_and_privacy(memory_stack, tmp_path):
    service, factory = memory_stack
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.CHAT_ONLY)
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: MemoryRunContext("r", "c", "b", "m")
    capabilities = gateway.runtime_capabilities("c")["capabilities"]
    assert capabilities["memory_read"]["status"] == "active"
    assert capabilities["memory_write"]["permission"] == "requires_confirmation"
    service.set_policy("c", MemoryPolicy.ALLOW)
    assert (
        gateway.runtime_capabilities("c")["capabilities"]["memory_write"]["permission"] == "allowed"
    )
    service.privacy.add("secret topic")
    snapshot = gateway.runtime_capabilities("c")
    assert snapshot["capabilities"]["scratchpad"]["status"] == "restricted"
    assert "secret topic" not in str(snapshot)
    assert not permissions.list_audit("c")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "active"),
        ({"LIMBOWAVE_ENVIRONMENT_PROVIDER": "invalid"}, "unconfigured"),
        ({"LIMBOWAVE_ENVIRONMENT_PROVIDER": "msn"}, "unconfigured"),
        (
            {
                "LIMBOWAVE_ENVIRONMENT_PROVIDER": "msn",
                "LIMBOWAVE_MSN_WEATHER_API_KEY": "secret-not-for-model",
            },
            "active",
        ),
    ],
)
def test_environment_reflects_backend_configuration_without_leaking_key(stack, env, expected):
    from limbowave.infrastructure.environment import create_environment_service

    gateway, _, _ = stack
    gateway._environment = create_environment_service(env)
    snapshot = gateway.runtime_capabilities("c")
    assert snapshot["capabilities"]["environment"]["status"] == expected
    assert "secret-not-for-model" not in str(snapshot)


def test_search_service_rechecks_configuration_and_key_presence(stack):
    from types import SimpleNamespace

    from limbowave.domain.network import NetworkSettings, SearchProvider
    from limbowave.infrastructure.network_backend import create_network_tools

    class Settings:
        config = NetworkSettings()

        def load(self):
            return SimpleNamespace(network=self.config)

    class Credentials:
        def __init__(self):
            self.refs = []

        def list_refs(self):
            return self.refs

        def resolve(self, _):
            pytest.fail("snapshot must not resolve a secret or call search")

    settings, credentials = Settings(), Credentials()
    gateway, _, _ = stack
    gateway._network = create_network_tools(settings, credentials)

    def status():
        return gateway.runtime_capabilities("c")["capabilities"]["web_search"]["status"]

    assert status() == "unconfigured"
    settings.config = NetworkSettings(
        search_provider=SearchProvider.TAVILY, credential_ref="secret-ref"
    )
    assert status() == "unconfigured"
    credentials.refs = ["secret-ref"]
    assert status() == "active"
    assert "secret-ref" not in str(gateway.runtime_capabilities("c"))
    credentials.refs = []
    assert status() == "unconfigured"
    gateway._network = create_network_tools(settings, None)
    assert status() == "unconfigured"


def test_full_access_snapshot_matches_memory_and_file_permissions(memory_stack, tmp_path):
    service, factory = memory_stack
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: MemoryRunContext("r", "c", "b", "m")
    snapshot = gateway.runtime_capabilities("c")["capabilities"]
    assert snapshot["memory_write"]["permission"] == "allowed"
    assert snapshot["memory_edit"]["permission"] == "allowed"
    assert snapshot["file_read"]["scope"] == "local_only"
    assert snapshot["file_write"]["scope"] == "local_only"
    assert not permissions.list_audit("c")
