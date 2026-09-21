"""路由服务的单元测试：确定性、可解释、不自动跨站点重发。"""

from __future__ import annotations

import pytest

from limbowave.application.services.routing_service import RoutingService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.routing import RoutingError


def _config() -> AppConfiguration:
    return AppConfiguration(
        endpoints=[
            EndpointConfig(
                id="relay-a",
                name="中转 A",
                base_url="https://a.example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
                credential_ref="key-a",
            ),
            EndpointConfig(
                id="relay-b",
                name="中转 B",
                base_url="https://b.example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            ),
        ],
        models=[
            LogicalModel(
                id="deepseek-chat",
                name="DeepSeek Chat",
                bindings=[
                    ModelBinding(endpoint_id="relay-a", model_id="deepseek-chat"),
                    ModelBinding(endpoint_id="relay-b", model_id="deepseek-chat"),
                ],
                default_binding=0,
            ),
            LogicalModel(
                id="kimi",
                name="Kimi",
                bindings=[ModelBinding(endpoint_id="relay-b", model_id="kimi-k2")],
            ),
        ],
        default_model_id="deepseek-chat",
    )


def test_route_default_picks_default_binding() -> None:
    decision = RoutingService(_config()).route()
    assert decision.model.id == "deepseek-chat"
    assert decision.endpoint.id == "relay-a"
    assert decision.provider_key == "relay-a"
    assert decision.model_id == "deepseek-chat"
    assert "默认绑定" in decision.reason  # 可解释


def test_route_is_deterministic() -> None:
    service = RoutingService(_config())
    a = service.route("deepseek-chat")
    b = service.route("deepseek-chat")
    assert a == b  # 同输入同输出


def test_route_explicit_model() -> None:
    decision = RoutingService(_config()).route("kimi")
    assert decision.endpoint.id == "relay-b"
    assert decision.model_id == "kimi-k2"


def test_route_missing_model_raises() -> None:
    with pytest.raises(RoutingError, match="不存在"):
        RoutingService(_config()).route("ghost")


def test_route_no_models_raises() -> None:
    with pytest.raises(RoutingError, match="未配置"):
        RoutingService(AppConfiguration()).route()


def test_no_silent_cross_endpoint_failover() -> None:
    """路由永远落在默认绑定；不存在"失败后换站"的隐式路径。"""
    decision = RoutingService(_config()).route("deepseek-chat")
    # 即便存在第二个绑定，默认也只选 default_binding=0 的那个
    assert decision.binding.endpoint_id == "relay-a"
    assert decision.binding is decision.model.bindings[0]
