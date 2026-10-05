"""Per-logical-model order replaces the legacy endpoint priority."""

from pathlib import Path

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository


def _service(path: Path) -> SettingsService:
    return SettingsService(ConfigurationService(JsonConfigRepository(path)))


@pytest.fixture
def service(tmp_path: Path) -> SettingsService:
    service = _service(tmp_path / "config.json")
    for index, site in enumerate(("a", "b", "c")):
        service.upsert_endpoint(EndpointConfig(
            id=site, name=site, base_url=f"https://{site}.example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS, priority=index * 10,
        ))
    service.upsert_model(LogicalModel(
        id="logical", name="Logical",
        context_window=256_000, max_tokens=16_384, supports_images=True,
        bindings=[ModelBinding(
            endpoint_id=site, model_id=f"remote-{site}",
            auto_matched=site == "a", supports_tools=site == "c",
        ) for site in ("a", "b", "c")],
    ))
    return service


def test_reorder_persists_and_routes_to_highest_priority(service, tmp_path):
    before = service.load().models[0]
    config = service.reorder_bindings("logical", ["c", "a", "b"])
    model = config.models[0]
    assert [b.endpoint_id for b in model.bindings] == ["c", "a", "b"]
    assert "default_binding" not in model.model_dump()
    assert RoutingService(config).route("logical").endpoint.id == "c"
    assert RoutingService(config).route("logical", endpoint_id="b").endpoint.id == "b"
    assert {b.key: b for b in model.bindings} == {b.key: b for b in before.bindings}
    assert model.context_window == before.context_window
    assert model.max_tokens == before.max_tokens
    assert model.supports_images is True
    assert _service(tmp_path / "config.json").load() == config


@pytest.mark.parametrize("order", [[], ["a", "b"], ["a", "a", "c"],
                                    ["a", "b", "c", "d"], ["a", "b", "missing"]])
def test_invalid_or_stale_order_does_not_write(service, tmp_path, order):
    path = tmp_path / "config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        service.reorder_bindings("logical", order)
    assert path.read_bytes() == before


def test_reorder_unknown_model_rejected(service):
    with pytest.raises(ValueError, match="不存在"):
        service.reorder_bindings("missing", [])


def test_empty_and_single_binding_order(service):
    service.upsert_model(LogicalModel(id="empty", name="Empty"))
    service.reorder_bindings("empty", [])
    service.bind_endpoint("empty", ModelBinding(endpoint_id="a", model_id="only"))
    config = service.reorder_bindings("empty", ["a"])
    model = next(m for m in config.models if m.id == "empty")
    assert RoutingService(config).route("empty").endpoint.id == "a"
    assert [b.endpoint_id for b in model.bindings] == ["a"]
    assert [b.endpoint_id for b in config.models[0].bindings] == ["a", "b", "c"]


def test_binding_edits_keep_custom_order(service):
    service.reorder_bindings("logical", ["c", "a", "b"])
    service.bind_endpoint("logical", ModelBinding(endpoint_id="a", model_id="replacement"))
    config = service.unbind_endpoint("logical", "c")
    assert [b.endpoint_id for b in config.models[0].bindings] == ["a", "b"]
    assert RoutingService(config).route("logical").endpoint.id == "a"
    config = service.bind_endpoint("logical", ModelBinding(endpoint_id="c", model_id="new"))
    assert [b.endpoint_id for b in config.models[0].bindings] == ["a", "b", "c"]


def test_auto_match_ignores_legacy_endpoint_priority(service):
    for site in ("c", "b", "a"):
        service.record_probed_model(
            endpoint_id=site, model_id="same", display_name="Same",
            default_thinking_level=None, thinking_level_locked=False,
            supports_thinking=None, supports_tools=False,
        )
    config, _plan = service.create_model(LogicalModel(id="same", name="Same"))
    model = next(m for m in config.models if m.id == "same")
    assert [b.endpoint_id for b in model.bindings] == ["a", "b", "c"]
    service.reorder_bindings("same", ["c", "a", "b"])
    config, _plan = service.auto_match("same")
    assert [b.endpoint_id for b in config.models[-1].bindings] == ["c", "a", "b"]


@pytest.mark.parametrize("legacy_default", [1, -1, 99])
def test_legacy_default_cannot_override_priority(service, tmp_path, legacy_default):
    import json

    path = tmp_path / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["models"][0]["default_binding"] = legacy_default
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = _service(path).load()
    assert RoutingService(config).route("logical").endpoint.id == "a"
    assert "default_binding" not in config.models[0].model_dump()
    service.upsert_model(config.models[0])
    assert "default_binding" not in json.loads(path.read_text(encoding="utf-8"))["models"][0]
