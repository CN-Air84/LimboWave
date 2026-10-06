"""LAN model adapter must reuse desktop routing, catalog refresh and kernel provenance."""
from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock, call

import pytest

from limbowave import runtime_composition
from limbowave.application.kernel import AgentKernel
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import ActualModel, LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol, RetryConfig
from limbowave.domain.routing import RoutingError


@pytest.fixture
def configured_catalog(tmp_path, monkeypatch):
    endpoint = EndpointConfig(
        id="private-site", name="Private endpoint", base_url="https://private.invalid/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS, credential_ref="private-key-ref",
        headers={"X-Private": "header-secret"}, timeout_seconds=42,
        retry=RetryConfig(max_attempts=2),
    )
    valid = LogicalModel(
        id="logical", name="Public model", supports_images=True, max_tokens=321,
        bindings=[ModelBinding(endpoint_id=endpoint.id, model_id="physical-model")],
    )
    config = AppConfiguration(
        endpoints=[endpoint, endpoint.model_copy(update={"id": "removed-site"})],
        models=[
            LogicalModel(id="unbound", name="Not configured"),
            # A usable second binding must NOT silently override a broken default.
            LogicalModel(id="broken", name="Broken default", bindings=[
                ModelBinding(endpoint_id="removed-site", model_id="gone"),
                ModelBinding(endpoint_id=endpoint.id, model_id="fallback-model"),
            ]),
            valid,
        ],
        actual_models=[ActualModel(
            endpoint_id=endpoint.id, model_id="physical-model",
            default_thinking_level="high", supports_thinking=True,
            available_thinking_levels=("off", "high"), supports_tools=True,
        )],
        default_model_id="logical",
    )
    # Deliberately simulate a stale in-memory snapshot after endpoint removal.
    # Normal AppConfiguration validation rejects dangling bindings at load time.
    config = config.model_copy(update={"endpoints": [endpoint]})
    settings = Mock(spec=SettingsService)
    settings.load.return_value = config
    credentials = Mock(spec=CredentialService)
    credentials.resolve.return_value = "endpoint-secret-sentinel"
    kernel = Mock(spec=AgentKernel)
    selected = Mock()
    get_kernel = Mock(return_value=kernel)
    # Spy on real catalog refresh: routing, credentials and derived files stay real.
    builder = runtime_composition.EnvironmentBuilder(tmp_path / "runtime")
    refresh = Mock(wraps=builder.refresh_catalog)
    monkeypatch.setattr(builder, "refresh_catalog", refresh)
    builder_factory = Mock(return_value=builder)
    monkeypatch.setattr(runtime_composition, "EnvironmentBuilder", builder_factory)
    catalog = runtime_composition.RuntimeModelCatalog(
        settings, credentials, tmp_path / "runtime", get_kernel, selected,
    )
    return catalog, settings, credentials, kernel, selected, get_kernel, refresh


def test_models_project_only_valid_routes_without_endpoint_or_secret(configured_catalog):
    catalog, settings, credentials, kernel, selected, get_kernel, refresh = configured_catalog
    before = settings.load.return_value.model_dump()
    result = catalog.models()
    assert result == [{"id": "logical", "name": "Public model", "supports_images": True}]
    serialized = json.dumps(result)
    for private in (
        "private-site", "private.invalid", "private-key-ref", "physical-model",
        "header-secret", "endpoint-secret-sentinel", "credential_ref", "bindings", "headers",
    ):
        assert private not in serialized
    credentials.resolve.assert_not_called()
    get_kernel.assert_not_called()
    selected.assert_not_called()
    refresh.assert_not_called()
    assert not kernel.mock_calls
    assert settings.load.return_value.model_dump() == before


def test_models_reload_current_desktop_configuration(configured_catalog):
    catalog, settings, *_ = configured_catalog
    assert catalog.models()
    settings.load.return_value = AppConfiguration()
    assert catalog.models() == []
    assert settings.load.call_count == 2


async def test_select_reuses_kernel_refreshes_catalog_and_commits_desktop_setup(configured_catalog):
    catalog, settings, credentials, kernel, selected, get_kernel, refresh = configured_catalog
    before = settings.load.return_value.model_dump()
    order = Mock()
    order.attach_mock(kernel.reload_models, "reload")
    order.attach_mock(kernel.set_model, "select")
    order.attach_mock(selected, "selected")
    order.attach_mock(kernel.set_thinking_level, "thinking")

    assert await catalog.select_model("logical") is None

    get_kernel.assert_called_once_with()
    refresh.assert_called_once()
    decisions = refresh.call_args.args[0]
    assert {(d.endpoint.id, d.model_id, secret) for d, secret in decisions} == {
        ("private-site", "physical-model", "endpoint-secret-sentinel"),
        ("private-site", "fallback-model", "endpoint-secret-sentinel"),
    }
    credentials.resolve.assert_has_calls([call("private-key-ref"), call("private-key-ref")])
    env, registered = kernel.reload_models.await_args.args
    assert "endpoint-secret-sentinel" in env.values()
    assert set(registered) == {
        ("private-site", "physical-model"), ("private-site", "fallback-model"),
    }
    kernel.reload_models.assert_awaited_once()
    kernel.set_model.assert_awaited_once_with("private-site", "physical-model")
    selected.assert_called_once()
    setup = selected.call_args.args[0]
    assert setup.kernel is kernel
    assert setup.logical_model_id == "logical"
    assert setup.endpoint_id == "private-site"
    assert "默认绑定" in setup.routing_reason
    assert setup.app_params == {"model": "physical-model", "max_tokens": 321}
    assert setup.retry_policy.max_attempts == 2
    assert setup.supports_images and setup.supports_tools and setup.supports_thinking
    assert setup.default_thinking_level == "high"
    assert setup.available_thinking_levels == ("off", "high")
    assert [item[0] for item in order.mock_calls] == ["reload", "select", "selected", "thinking"]
    kernel.set_thinking_level.assert_awaited_once_with("high")
    assert settings.load.return_value.model_dump() == before


async def test_unchanged_catalog_skips_reload_but_still_selects(configured_catalog):
    catalog, settings, _, kernel, selected, _, refresh = configured_catalog
    await catalog.select_model("logical")
    await catalog.select_model("logical")
    assert settings.load.call_count == refresh.call_count == 2
    kernel.reload_models.assert_awaited_once()
    assert kernel.set_model.await_count == selected.call_count == 2


async def test_changed_credentials_refresh_existing_kernel(configured_catalog):
    catalog, _, credentials, kernel, _, get_kernel, _ = configured_catalog
    await catalog.select_model("logical")
    credentials.resolve.return_value = "rotated-secret"
    await catalog.select_model("logical")
    assert kernel.reload_models.await_count == 2
    assert "rotated-secret" in kernel.reload_models.await_args.args[0].values()
    assert kernel.set_model.await_count == get_kernel.call_count == 2


@pytest.mark.parametrize("missing", ["kernel", "credentials"])
async def test_missing_runtime_rejected_before_io(configured_catalog, tmp_path, missing):
    _, settings, credentials, kernel, selected, _, refresh = configured_catalog
    catalog = runtime_composition.RuntimeModelCatalog(
        settings, None if missing == "credentials" else credentials, tmp_path,
        lambda: None if missing == "kernel" else kernel, selected,
    )
    with pytest.raises(RuntimeError, match=r"^runtime_unavailable$"):
        await catalog.select_model("logical")
    settings.load.assert_not_called()
    credentials.resolve.assert_not_called()
    refresh.assert_not_called()
    selected.assert_not_called()
    assert not kernel.mock_calls


@pytest.mark.parametrize("model_id", ["unknown", "unbound", "broken"])
async def test_invalid_route_never_falls_back_or_changes_kernel(configured_catalog, model_id):
    catalog, _, credentials, kernel, selected, _, refresh = configured_catalog
    with pytest.raises(RoutingError):
        await catalog.select_model(model_id)
    credentials.resolve.assert_not_called()
    refresh.assert_not_called()
    selected.assert_not_called()
    assert not kernel.mock_calls


async def test_reload_failure_does_not_select_and_is_retried(configured_catalog):
    catalog, _, _, kernel, selected, _, _ = configured_catalog
    kernel.reload_models.side_effect = RuntimeError("reload failed")
    with pytest.raises(RuntimeError, match="reload failed"):
        await catalog.select_model("logical")
    kernel.set_model.assert_not_awaited()
    selected.assert_not_called()
    kernel.reload_models.side_effect = None
    await catalog.select_model("logical")
    assert kernel.reload_models.await_count == 2


async def test_selection_failure_does_not_commit_provenance(configured_catalog):
    catalog, _, _, kernel, selected, _, _ = configured_catalog
    kernel.set_model.side_effect = RuntimeError("selection failed")
    with pytest.raises(RuntimeError, match="selection failed"):
        await catalog.select_model("logical")
    selected.assert_not_called()
    kernel.set_thinking_level.assert_not_awaited()


async def test_thinking_failure_keeps_already_selected_provenance(configured_catalog):
    catalog, _, _, kernel, selected, _, _ = configured_catalog
    kernel.set_thinking_level.side_effect = RuntimeError("thinking failed")
    with pytest.raises(RuntimeError, match="thinking failed"):
        await catalog.select_model("logical")
    selected.assert_called_once()
    assert selected.call_args.args[0].kernel is kernel
    kernel.set_model.assert_awaited_once_with("private-site", "physical-model")


def test_import_does_not_require_qt():
    # Fresh interpreter avoids false positives from other tests already importing Qt.
    code = """
import importlib.abc
import sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'PyQt6', 'PyQt5', 'qasync'}:
            raise AssertionError('Qt import: ' + fullname)
sys.meta_path.insert(0, NoQt())
from limbowave.runtime_composition import RuntimeModelCatalog
assert RuntimeModelCatalog
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

async def test_select_uses_updated_authoritative_binding_and_capabilities(configured_catalog):
    catalog, settings, _, kernel, selected, _, refresh = configured_catalog
    await catalog.select_model("logical")
    kernel.reset_mock()
    selected.reset_mock()
    original = settings.load.return_value
    # A desktop edit is authoritative; do not cache the previous physical model or
    # reuse its thinking-level default when the new actual model has none.
    settings.load.return_value = AppConfiguration(
        endpoints=original.endpoints,
        models=[LogicalModel(
            id="logical", name="Updated public model",
            bindings=[ModelBinding(endpoint_id="private-site", model_id="new-physical")],
        )],
        actual_models=[ActualModel(
            endpoint_id="private-site", model_id="new-physical", supports_tools=False,
        )],
        default_model_id="logical",
    )
    await catalog.select_model("logical")
    assert refresh.call_count == 2
    kernel.reload_models.assert_awaited_once()
    assert kernel.reload_models.await_args.args[1] == (("private-site", "new-physical"),)
    kernel.set_model.assert_awaited_once_with("private-site", "new-physical")
    kernel.set_thinking_level.assert_not_awaited()
    setup = selected.call_args.args[0]
    assert setup.kernel is kernel
    assert setup.app_params == {"model": "new-physical"}
    assert not setup.supports_tools
    assert not setup.supports_images
    assert setup.default_thinking_level is None
