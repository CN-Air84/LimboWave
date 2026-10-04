"""站点覆盖必须同时更新真实模型与能力，思考偏好不随历史分支漂移。"""

from dataclasses import replace

import pytest

from limbowave.app import _run_context
from limbowave.application.services.routing_service import RoutingService
from limbowave.composition import kernel_setup
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.routing import RoutingError
from tests.unit.test_run_coordinator import FakeKernel


def test_override_and_clear_update_remote_id_not_only_endpoint() -> None:
    config = AppConfiguration(
        endpoints=[
            EndpointConfig(
                id=key,
                name=key,
                base_url="https://example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
            for key in ("default", "alternate")
        ],
        models=[
            LogicalModel(
                id="logical",
                name="Logical",
                supports_images=True,
                bindings=[
                    ModelBinding(endpoint_id="default", model_id="remote-a"),
                    ModelBinding(endpoint_id="alternate", model_id="remote-b-high"),
                ],
            )
        ],
    )
    routing = RoutingService(config)
    kernel = FakeKernel()
    state = {"value": kernel_setup(kernel, routing.route("logical"))}
    override = {"endpoint": None}
    get_context = _run_context(state, override)
    assert get_context().app_params["model"] == "remote-a"
    decision = routing.route("logical", endpoint_id="alternate")
    state["value"] = kernel_setup(kernel, decision)
    override["endpoint"] = "alternate"
    assert get_context().endpoint_id == "alternate"
    assert get_context().app_params["model"] == "remote-b-high"
    assert "default" in get_context().routing_reason
    state["value"] = kernel_setup(kernel, routing.route("logical"))
    override["endpoint"] = None
    assert get_context().endpoint_id == "default"
    assert get_context().app_params["model"] == "remote-a"
    with pytest.raises(RoutingError, match="没有绑定"):
        routing.route("logical", endpoint_id="missing")


@pytest.mark.parametrize(
    ("supported", "locked", "levels", "expected"),
    [
        (True, False, (), "high"),
        (False, False, (), "off"),
        (True, True, (), "low"),
        (True, False, ("low",), "low"),
    ],
)
def test_requested_thinking_respects_binding_capabilities(
    supported, locked, levels, expected
) -> None:
    from limbowave.application.kernel import KernelSetup

    setup = KernelSetup(
        kernel=FakeKernel(),
        logical_model_id="logical",
        endpoint_id="endpoint",
        routing_reason="test",
        app_params={"model": "remote"},
        default_thinking_level="low",
        supports_thinking=supported,
        thinking_level_locked=locked,
        available_thinking_levels=levels,
    )
    state = {"value": setup}
    desired = {"level": "high"}
    get_context = _run_context(state, thinking=desired)
    assert get_context().thinking_level == expected
    state["value"] = replace(
        setup, supports_thinking=True, thinking_level_locked=False, available_thinking_levels=()
    )
    desired["level"] = "off"
    assert get_context().thinking_level == "off"
