from dataclasses import replace

import pytest

from limbowave.app import _run_context
from limbowave.application.kernel import KernelSetup
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.application.services.thinking_trial import ThinkingTrial
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel


def trial():
    setup = KernelSetup(FakeKernel(), "logical", "site", "test", app_params={"model": "remote"})
    return ThinkingTrial(setup, "remote", "max", "off")


def result(t):
    return {"trial_id": t.id, "endpoint_id": "site", "model_id": "remote", "level": "max",
            "status": "completed", "request_sent": True}


def test_success_is_offered_once_without_persisting():
    t = trial()
    assert t.accept_success(result(t))
    assert not t.accept_success(result(t))


@pytest.mark.parametrize("change", [
    {"trial_id": "old"}, {"endpoint_id": "other"}, {"model_id": "other"},
    {"level": "high"}, {"status": "failed"}, {"status": "aborted"},
    {"status": "interrupted"}, {"request_sent": False},
])
def test_unrelated_failed_or_unsent_runs_never_offer(change):
    t = trial()
    assert not t.accept_success({**result(t), **change})
    assert not t.offered


async def test_finalized_foreground_run_carries_trial_identity():
    t = trial()
    setup = replace(t.base_setup, thinking_trial_id=t.id, supports_thinking=True)
    context = _run_context(setup, thinking={"level": "max"})
    coordinator = RunCoordinator(setup.kernel, in_memory_uow_factory(), context=context)
    events = []
    coordinator.subscribe(events.append)
    await coordinator.send("try")
    kernel = setup.kernel
    kernel.observe({"kind": "provider.request", "payload": {"model": "remote"}})
    kernel.say("ok")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    completed = [e for e in events if e.kind == "thinking_trial_finished"]
    assert len(completed) == 1
    assert t.accept_success(completed[0].data)


@pytest.mark.parametrize("outcome", ["failed", "aborted"])
async def test_failed_and_aborted_runs_do_not_offer_confirmation(outcome):
    t = trial()
    setup = replace(t.base_setup, thinking_trial_id=t.id, supports_thinking=True)
    coordinator = RunCoordinator(setup.kernel, in_memory_uow_factory(),
                                 context=_run_context(setup, thinking={"level": "max"}))
    events = []
    coordinator.subscribe(events.append)
    kernel = setup.kernel
    if outcome == "failed":
        kernel.send_error = RuntimeError("request rejected")
    await coordinator.send("try")
    if outcome == "aborted":
        kernel.observe({"kind": "provider.request", "payload": {"model": "remote"}})
        await coordinator.abort()
        kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    completed = [e for e in events if e.kind == "thinking_trial_finished"]
    assert len(completed) == 1
    if outcome == "aborted":
        assert completed[0].data["cancelled"] is True
    else:
        assert completed[0].data["status"] == outcome
    assert not t.accept_success(completed[0].data)
