"""Exercise the actual app callbacks: one atomic model/site selection, no default detour."""

import ast
from collections.abc import Awaitable, Callable
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


def _route_refresh(runtime):
    from dataclasses import replace

    ns, kernel, config = runtime
    ns.update(replace=replace, thinking_trial=None, Callable=Callable, Awaitable=Awaitable)
    return ns, kernel, config, _callback("_refresh_current_route", ns)


def test_route_refresh_updates_rebound_actual_model_without_changing_endpoint(runtime):
    ns, _kernel, config, refresh = _route_refresh(runtime)
    config.models[0].bindings[0] = ModelBinding(
        endpoint_id="a", model_id="new-physical-model", supports_thinking=False
    )
    assert refresh()
    assert ns["setup"].app_params["model"] == "new-physical-model"
    assert ns["setup"].endpoint_id == "a"
    assert ns["setup"].supports_thinking is False
    assert ns["setup_state"]["value"] is ns["setup"]


@pytest.mark.parametrize("removed", ["model", "endpoint", "binding"])
def test_route_refresh_rejects_removed_target_without_fallback(runtime, removed):
    ns, kernel, config, refresh = _route_refresh(runtime)
    original = ns["setup"]
    if removed == "model":
        config.models.pop(0)
    elif removed == "endpoint":
        config.endpoints.pop(0)
    else:
        config.models[0].bindings[:] = [ModelBinding(endpoint_id="b", model_id="other")]
    assert not refresh()
    assert ns["setup"] is original
    assert ns["setup_state"]["value"] is original
    kernel.set_model.assert_not_awaited()
    ns["chat"].set_status.assert_called_once()
    assert "重新选择" in ns["chat"].set_status.call_args.args[0]


def test_route_refresh_preserves_unchanged_thinking_trial(runtime):
    from dataclasses import replace

    from limbowave.application.services.thinking_trial import ThinkingTrial

    ns, _kernel, _config, refresh = _route_refresh(runtime)
    trial = ThinkingTrial(ns["setup"], "gpt-5", "high", "off")
    ns["thinking_trial"] = trial
    ns["setup"] = replace(
        ns["setup"], thinking_trial_id=trial.id, supports_thinking=True,
        available_thinking_levels=("high",), runtime_thinking_levels=("off", "high"),
    )
    original = ns["setup"]
    assert refresh()
    assert ns["setup"] is original
    ns["_clear_thinking_trial"].assert_not_called()


@pytest.mark.parametrize("operation", ["send", "retry", "edit", "regenerate"])
async def test_all_desktop_sends_refresh_route_and_catalog_before_acceptance(runtime, operation):
    from typing import Any

    ns, kernel, config, _refresh = _route_refresh(runtime)
    ns["Any"] = Any
    ns["detached_scope"] = None
    ns["chat"].history_loading = False
    ns["controller"].coordinator().command_scope = nullcontext
    ns["controller"].send = AsyncMock(return_value="run")
    config.models[0].bindings[0] = ModelBinding(endpoint_id="a", model_id="replacement")
    send = _callback("_send_with_catalog_sync", ns)
    action = None if operation == "send" else AsyncMock(return_value="run")
    kwargs = {} if action is None else {"send_action": action}
    assert await send("hello", **kwargs)
    ns["_sync_model_catalog"].assert_awaited_once()
    assert ns["setup"].app_params["model"] == "replacement"
    if action is None:
        ns["controller"].send.assert_awaited_once_with("hello")
    else:
        action.assert_awaited_once_with()
        ns["controller"].send.assert_not_awaited()
    kernel.set_model.assert_not_awaited()  # RunCoordinator owns the final RPC preflight.


async def test_send_rejects_removed_route_before_saving_message(runtime):
    from typing import Any

    ns, _kernel, config, _refresh = _route_refresh(runtime)
    ns["Any"] = Any
    ns["detached_scope"] = None
    ns["chat"].history_loading = False
    ns["controller"].coordinator().command_scope = nullcontext
    ns["controller"].send = AsyncMock(return_value="run")
    config.models.pop(0)
    assert not await _callback("_send_with_catalog_sync", ns)("keep my draft")
    ns["controller"].send.assert_not_awaited()
    ns["_sync_model_catalog"].assert_not_awaited()


async def test_reselect_current_model_recovers_removed_endpoint_only_on_explicit_click(runtime):
    ns, kernel, config = runtime
    config.models[0].bindings[:] = [ModelBinding(endpoint_id="b", model_id="new-default")]
    await _callback("_apply_logical_model", ns)("gpt")
    kernel.set_model.assert_awaited_once_with("b", "new-default")
    assert ns["setup"].endpoint_id == "b"
    assert ns["setup_state"]["value"] is ns["setup"]


async def test_send_after_config_change_uses_refreshed_request_context(runtime):
    from typing import Any

    from limbowave.app import _run_context

    ns, _kernel, config, _refresh = _route_refresh(runtime)
    ns["Any"] = Any
    ns["detached_scope"] = None
    ns["chat"].history_loading = False
    ns["controller"].coordinator().command_scope = nullcontext
    context = _run_context(ns["setup_state"], ns["session_endpoint_override"])
    captured = []

    async def accept(_text):
        captured.append(context())
        return "run"

    ns["controller"].send = accept
    config.models[0].bindings[0] = ModelBinding(endpoint_id="a", model_id="new-physical")
    assert await _callback("_send_with_catalog_sync", ns)("hello")
    assert captured[0].logical_model_id == "gpt"
    assert captured[0].endpoint_id == "a"
    assert captured[0].app_params["model"] == "new-physical"


async def test_removed_model_can_be_reselected_and_sent_with_real_controller(runtime, qtbot):
    from typing import Any

    from limbowave.app import _run_context
    from limbowave.application.services.session_controller import SessionController
    from limbowave.ui.chat_view import ChatView
    from tests.unit.test_run_coordinator import FakeKernel

    ns, _kernel, config = runtime
    kernel = FakeKernel()
    setup = kernel_setup(kernel, RoutingService(config).route("gpt"))
    ns["setup"] = ns["setup_state"]["value"] = setup
    controller = SessionController(kernel, context=_run_context(ns["setup_state"]))
    view = ChatView()
    qtbot.addWidget(view)
    ns.update(controller=controller, chat=view, detached_scope=None, Any=Any)
    _route_refresh(runtime)
    _callback("_refresh_toolbar", ns)()
    send = _callback("_send_with_catalog_sync", ns)
    apply = _callback("_apply_logical_model", ns)
    await controller.start()
    try:
        config.models.pop(0)
        ns["_refresh_toolbar"]()
        assert view._logical_model.currentData() is None
        assert not await send("not sent to stale gpt")
        assert controller.conversation_id is None
        assert not kernel.sent
        with qtbot.waitSignal(view.logical_model_changed) as signal:
            view._logical_model.setCurrentIndex(view._logical_model.findData("step"))
        await apply(signal.args[0])
        assert await send("send to selected model")
        assert view._logical_model.currentData() == "step"
        assert kernel.provider == "c"
        assert kernel.model_id == "step-3"
        assert kernel.sent == ["send to selected model"]
        kernel.say("done")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
    finally:
        await controller.shutdown()
