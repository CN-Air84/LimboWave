"""权威配置的领域校验单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol


def _endpoint(id: str = "ep1") -> EndpointConfig:
    return EndpointConfig(
        id=id,
        name="站点一",
        base_url="https://api.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
        credential_ref="key-ep1",
    )


def _model(id: str = "m1", endpoint_id: str = "ep1") -> LogicalModel:
    return LogicalModel(
        id=id,
        name="模型一",
        bindings=[ModelBinding(endpoint_id=endpoint_id, model_id="real-model-x")],
    )


def test_valid_config() -> None:
    config = AppConfiguration(endpoints=[_endpoint()], models=[_model()], default_model_id="m1")
    assert config.endpoints[0].id == "ep1"
    assert config.models[0].bindings[0].model_id == "real-model-x"


def test_duplicate_endpoint_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfiguration(endpoints=[_endpoint("ep1"), _endpoint("ep1")])


def test_binding_to_missing_endpoint_rejected() -> None:
    with pytest.raises(ValidationError, match="不存在的端点"):
        AppConfiguration(endpoints=[_endpoint()], models=[_model(endpoint_id="ghost")])


def test_default_model_must_exist() -> None:
    with pytest.raises(ValidationError, match="default_model_id"):
        AppConfiguration(endpoints=[_endpoint()], models=[_model()], default_model_id="ghost")


def test_legacy_default_binding_is_ignored() -> None:
    legacy = {**_model().model_dump(), "default_binding": 5}
    model = LogicalModel.model_validate(legacy)
    config = AppConfiguration(endpoints=[_endpoint()], models=[model])
    assert config.models[0].bindings[0].endpoint_id == "ep1"
    assert "default_binding" not in config.model_dump()["models"][0]


def test_base_url_must_be_url() -> None:
    with pytest.raises(ValidationError):
        EndpointConfig(
            id="x", name="x", base_url="not-a-url", api=ProviderProtocol.OPENAI_COMPLETIONS
        )


def test_config_has_no_secret_field() -> None:
    """架构守卫：端点配置不允许出现密钥本体字段（只允许 credential_ref 引用）。"""
    config = AppConfiguration(endpoints=[_endpoint()], models=[_model()])
    dumped = config.model_dump(mode="json")
    for endpoint in dumped["endpoints"]:
        assert "credential_ref" in endpoint
        for forbidden in ("api_key", "apiKey", "secret", "key", "token", "password"):
            assert forbidden not in endpoint, f"端点配置含密钥本体字段：{forbidden}"


def test_endpoint_rpm_defaults_for_legacy_configuration() -> None:
    payload = _endpoint().model_dump(mode="json")
    payload.pop("rpm")
    assert EndpointConfig.model_validate(payload).rpm == 5
    payload["rpm"] = 42
    assert EndpointConfig.model_validate_json(
        EndpointConfig.model_validate(payload).model_dump_json()
    ).rpm == 42


@pytest.mark.parametrize("rpm", [0, -1, 60_001, 1.5, True, "5"])
def test_endpoint_rpm_rejects_invalid_limits(rpm: object) -> None:
    with pytest.raises(ValidationError):
        EndpointConfig.model_validate({**_endpoint().model_dump(), "rpm": rpm})
