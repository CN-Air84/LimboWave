"""显式批量解绑并删除：单次保存，不删除逻辑模型或其他站点配置。"""

from unittest.mock import Mock

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol


def _config() -> AppConfiguration:
    def model(name: str, sites: list[str]) -> LogicalModel:
        return LogicalModel(
            id=name, name=name,
            bindings=[ModelBinding(endpoint_id=site, model_id=name) for site in sites],
        )

    return AppConfiguration(
        endpoints=[
            EndpointConfig(
                id=site, name=site, base_url=f"https://{site}.example/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
            for site in ("a", "b", "c")
        ],
        models=[
            model("only-a", ["a"]),
            model("default-c", ["c", "a", "b"]),
            model("default-a", ["a", "b", "c"]),
            model("untouched", ["c", "b"]),
        ],
        default_model_id="only-a",
    )


def test_unbind_delete_is_one_validated_save() -> None:
    before = _config()
    repo = Mock()
    repo.load.return_value = before
    service = SettingsService(ConfigurationService(repo))

    after = service.delete_endpoint("a", unbind_models=True)

    repo.save.assert_called_once_with(after)
    assert [e.id for e in after.endpoints] == ["b", "c"]
    assert all(a.endpoint_id != "a" for a in after.actual_models)
    assert after.actual_models == [
        a for a in before.actual_models if a.endpoint_id != "a"
    ]
    assert [m.id for m in after.models] == [m.id for m in before.models]
    assert after.models[0].bindings == []
    assert [b.endpoint_id for b in after.models[1].bindings] == ["c", "b"]
    assert RoutingService(after).route("default-c").endpoint.id == "c"
    assert RoutingService(after).route("default-a").endpoint.id == "b"
    assert after.models[3] == before.models[3]
    assert after.default_model_id == before.default_model_id
    assert after.compression == before.compression
    assert after.memory == before.memory
    assert len(before.models[0].bindings) == 1  # 输入配置不可变


def test_bulk_delete_still_requires_explicit_opt_in() -> None:
    repo = Mock()
    repo.load.return_value = _config()
    service = SettingsService(ConfigurationService(repo))
    with pytest.raises(ValueError, match="仍被模型绑定"):
        service.delete_endpoint("a")
    repo.save.assert_not_called()


def test_bulk_delete_save_failure_does_not_partially_unbind() -> None:
    before = _config()
    repo = Mock()
    repo.load.return_value = before
    repo.save.side_effect = OSError("disk full")
    service = SettingsService(ConfigurationService(repo))
    with pytest.raises(OSError, match="disk full"):
        service.delete_endpoint("a", unbind_models=True)
    repo.save.assert_called_once()
    assert service.load() == before
    assert len(before.models[0].bindings) == 1
