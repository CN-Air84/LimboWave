"""Confirmed endpoint failover is a retry, not a new chat turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.ui.test_model_selector_routing import _callback


@pytest.fixture
def failover():
    coordinator = SimpleNamespace(branch_id="branch-1")
    controller = SimpleNamespace(
        conversation_id="conversation-1", busy=False, coordinator=lambda: coordinator,
        send=AsyncMock(), retry_user_message=AsyncMock(), wait_idle=AsyncMock(),
    )
    tasks = []
    ns = {
        "controller": controller,
        "chat": Mock(), "window": Mock(), "NL": "\n",
        "_failover_candidates": lambda: [("backup", "model")],
        "_apply_endpoint_override": AsyncMock(return_value=True),
        "_last_user_message_id": lambda: "user-1",
        "run_blocking": AsyncMock(side_effect=lambda f: f()),
        "_spawn": tasks.append, "ask_confirm": Mock(),
    }
    return ns, controller, tasks


async def test_confirmed_failover_retries_original_message_not_text(failover):
    ns, controller, tasks = failover
    offer = _callback("_offer_failover_if_available", ns)
    offer("user-1")
    ns["ask_confirm"].call_args.args[3](True)
    await tasks.pop()
    controller.retry_user_message.assert_awaited_once_with("user-1")
    controller.send.assert_not_awaited()
    ns["_apply_endpoint_override"].assert_awaited_once_with("backup")


@pytest.mark.parametrize("change", ["declined", "conversation", "branch", "new_turn", "busy",
                                    "switch_failed", "switched_while_awaiting"])
async def test_failover_never_sends_a_new_turn_when_confirmation_is_stale(failover, change):
    ns, controller, tasks = failover
    _callback("_offer_failover_if_available", ns)("user-1")
    if change == "conversation":
        controller.conversation_id = "conversation-2"
    elif change == "branch":
        controller.coordinator().branch_id = "branch-2"
    elif change == "new_turn":
        ns["_last_user_message_id"] = lambda: "user-2"
    elif change == "busy":
        controller.busy = True
    elif change == "switch_failed":
        ns["_apply_endpoint_override"].return_value = False
    elif change == "switched_while_awaiting":
        async def switch(_endpoint):
            controller.conversation_id = "conversation-2"
            return True
        ns["_apply_endpoint_override"].side_effect = switch
    ns["ask_confirm"].call_args.args[3](change != "declined")
    for task in tasks:
        await task
    controller.retry_user_message.assert_not_awaited()
    controller.send.assert_not_awaited()
