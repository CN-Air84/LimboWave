"""Exercise the actual app callbacks: one atomic model/site selection, no default detour."""

import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from limbowave.application.services.routing_service import RoutingService
from limbowave.composition import kernel_setup
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.ui.model_selector import ModelSite


def _callback(name, namespace):
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    # Lift the closure into a test namespace without replacing any callback behavior.
    function.body = [
        ast.copy_location(ast.Global(node.names), node) if isinstance(node, ast.Nonlocal) else node
        for node in function.body
    ]
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


@pytest.fixture
def runtime():
    config = AppConfiguration(
        endpoints=[
            EndpointConfig(
                id=key,
                name=f"站点 {key}",
                base_url="https://example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
            for key in ("a", "b", "c")
        ],
        models=[
            LogicalModel(
                id="gpt",
                name="主力",
                bindings=[
                    ModelBinding(endpoint_id="a", model_id="gpt-5"),
                ],
            ),
            LogicalModel(
                id="step",
                name="快速模型",
                bindings=[
                    ModelBinding(endpoint_id="c", model_id="step-3"),
                    ModelBinding(
                        endpoint_id="b",
                        model_id="step-3-fast",
                        supports_tools=True,
                        supports_thinking=True,
                        default_thinking_level="high",
                    ),
                ],
            ),
        ],
    )
    kernel = SimpleNamespace(set_model=AsyncMock())
    setup = kernel_setup(kernel, RoutingService(config).route("gpt"))
    coordinator = SimpleNamespace(kernel=kernel, runtime_transition=nullcontext)
    namespace = {
        "setup": setup,
        "setup_state": {"value": setup},
        "controller": SimpleNamespace(busy=False, coordinator=lambda: coordinator),
        "settings": SimpleNamespace(load=lambda: config),
        "chat": Mock(),
        "toolbar": Mock(),
        "session_endpoint_override": {"endpoint": None},
        "_refresh_toolbar": Mock(),
        "_sync_toolbar_route_candidates": Mock(),
        "_sync_model_catalog": AsyncMock(return_value=True),
        "_apply_binding_defaults": AsyncMock(),
        "_clear_thinking_trial": Mock(),
        "ModelSite": ModelSite,
    }
    return namespace, kernel, config


async def test_select_other_models_alternate_site_is_one_kernel_transition(runtime):
    ns, kernel, _config = runtime
    apply = _callback("_apply_logical_model", ns)
    await apply("step", "b")
    kernel.set_model.assert_awaited_once_with("b", "step-3-fast")
    assert ns["setup"].logical_model_id == "step"
    assert ns["setup"].endpoint_id == "b"
    assert ns["setup"].supports_tools
    assert ns["setup"].default_thinking_level == "high"
    assert ns["setup_state"]["value"] is ns["setup"]
    assert ns["session_endpoint_override"]["endpoint"] == "b"
    ns["_apply_binding_defaults"].assert_awaited_once()


async def test_current_model_can_change_sites_then_restore_highest_priority_default(runtime):
    ns, kernel, config = runtime
    ns["setup"] = kernel_setup(kernel, RoutingService(config).route("step"))
    apply = _callback("_apply_logical_model", ns)
    await apply("step", "b")
    assert ns["session_endpoint_override"]["endpoint"] == "b"
    await apply("step", "")
    assert kernel.set_model.await_args_list[0].args == ("b", "step-3-fast")
    assert kernel.set_model.await_args_list[1].args == ("c", "step-3")
    assert ns["session_endpoint_override"]["endpoint"] is None
    assert ns["setup"].endpoint_id == "c"


async def test_ordinary_current_model_click_preserves_existing_override(runtime):
    ns, kernel, config = runtime
    ns["setup"] = kernel_setup(kernel, RoutingService(config).route("step", endpoint_id="b"))
    ns["session_endpoint_override"]["endpoint"] = "b"
    await _callback("_apply_logical_model", ns)("step")
    kernel.set_model.assert_not_awaited()
    assert ns["session_endpoint_override"]["endpoint"] == "b"


@pytest.mark.parametrize("failure", ["busy", "route", "catalog", "kernel", "unconfigured"])
async def test_failed_selection_keeps_previous_model_and_refreshes_ui(runtime, failure):
    ns, kernel, _config = runtime
    original = ns["setup"]
    endpoint = "b"
    if failure == "busy":
        ns["controller"].busy = True
    elif failure == "route":
        endpoint = "not-bound"
    elif failure == "catalog":
        ns["_sync_model_catalog"].return_value = False
    elif failure == "kernel":
        kernel.set_model.side_effect = RuntimeError("offline")
    else:
        ns["controller"].coordinator().kernel = None
    await _callback("_apply_logical_model", ns)("step", endpoint)
    assert ns["setup"] is original
    assert ns["session_endpoint_override"]["endpoint"] is None
    ns["_refresh_toolbar"].assert_called_once()
    ns["_apply_binding_defaults"].assert_not_awaited()
    if failure != "kernel":
        kernel.set_model.assert_not_awaited()


@pytest.mark.parametrize("configured", [True, False])
def test_toolbar_refresh_supplies_all_models_sites_not_just_current(runtime, configured):
    ns, _kernel, _config = runtime
    if not configured:
        ns["setup"] = None
    _callback("_refresh_toolbar", ns)()
    call = ns["chat"].set_logical_models.call_args
    sites = call.kwargs["sites"]
    assert sites["gpt"][0].name == "站点 a"
    assert [site.endpoint_id for site in sites["step"]] == ["c", "b"]
    assert sites["step"][0].default
    assert not sites["step"][1].default
    assert sites["step"][1].model_id == "step-3-fast"
    if configured:
        assert call.args[1] == "gpt"
        assert call.kwargs["current_endpoint"] == "a"
