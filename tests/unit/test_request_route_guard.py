"""最终发送路由、分叉恢复与同图重试的回归。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from limbowave.application.services.attachment_service import AttachmentPayload
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.retry import RetryPolicy
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import BranchingKernel, FakeKernel, _seed_one_turn

IMAGES = [{"type": "image", "data": "original-image", "mimeType": "image/png"}]


def context() -> RunContext:
    return RunContext(
        logical_model_id="flash",
        endpoint_id="relay-b",
        routing_reason="selected",
        app_params={"model": "flash-remote", "max_tokens": 100},
        thinking_level="off",
        supports_images=True,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_ms=0),
    )


async def finish(kernel: FakeKernel, coord: RunCoordinator) -> None:
    kernel.say("ACK")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


class ResettingKernel(BranchingKernel):
    async def fork(self, entry_id: str) -> str:
        result = await super().fork(entry_id)
        self.model_id, self.provider, self.thinking_level = "old-pro", "relay-a", "high"
        return result


async def test_edit_after_model_switch_reapplies_route_and_thinking() -> None:
    kernel, store = ResettingKernel(), InMemoryStore()
    _seed_one_turn(kernel)
    selected = replace(context(), endpoint_id="relay-a", app_params={"model": "old-pro"})
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: selected)
    first = await coord.send("第一轮")
    await finish(kernel, coord)
    selected = context()
    second = await coord.edit_user_message(
        store.runs[first].user_message_id,
        "edited",
        attachment_ids=["image"],
        images=IMAGES,
    )
    assert second != first
    assert kernel.forked == ["e1"]
    assert (kernel.provider, kernel.model_id, kernel.thinking_level) == (
        "relay-b",
        "flash-remote",
        "off",
    )
    assert kernel.sent_images[-1] == IMAGES
    await finish(kernel, coord)


@pytest.mark.parametrize("failure", ["set", "read", "provider", "model", "missing", "thinking"])
async def test_route_failure_never_sends_or_retries(failure: str) -> None:
    class BrokenKernel(FakeKernel):
        async def set_model(self, provider: str, model_id: str) -> None:
            if failure == "set":
                raise RuntimeError("ECONNRESET during set_model")
            await super().set_model(provider, model_id)

        async def get_state(self):
            if failure == "read":
                raise RuntimeError("state timeout")
            state = await super().get_state()
            if failure == "provider":
                return replace(state, provider="wrong")
            if failure == "model":
                return replace(state, model_id="wrong")
            if failure == "missing":
                return replace(state, provider=None)
            if failure == "thinking":
                return replace(state, thinking_level="high")
            return state

    kernel, store = BrokenKernel(), InMemoryStore()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=context)
    run = await coord.send("do not send")
    await coord.wait_idle()
    assert not kernel.sent
    assert not coord.busy
    assert store.runs[run].status is RunStatus.FAILED
    assert "路由校验失败" in store.runs[run].error


async def test_auto_retry_preserves_images_and_frozen_route() -> None:
    kernel, store = FakeKernel(), InMemoryStore()
    selected = context()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: selected)
    run = await coord.send("picture", attachment_ids=["image"], images=IMAGES)
    selected.app_params["model"] = "mutated-model"
    selected = replace(context(), endpoint_id="other", retry_policy=RetryPolicy(max_attempts=1))
    kernel.provider, kernel.model_id = "reset", "reset"
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "ECONNRESET",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert kernel.sent == ["picture", "picture"]
    assert kernel.sent_images == [IMAGES, IMAGES]
    assert (kernel.provider, kernel.model_id) == ("relay-b", "flash-remote")
    await finish(kernel, coord)
    assert store.runs[run].status is RunStatus.COMPLETED


@pytest.mark.parametrize("mode", ["manual", "regenerate_failed", "regenerate_complete"])
async def test_retries_rebuild_original_attachments(mode: str) -> None:
    kernel, store = BranchingKernel(), InMemoryStore()
    _seed_one_turn(kernel)
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=context)
    calls = []

    def build(ids):
        calls.append(ids)
        return AttachmentPayload(attachment_ids=ids, images=IMAGES)

    coord.attachment_builder = build
    run = await coord.send("第一轮", attachment_ids=["image"], images=IMAGES)
    kernel.say("ACK", stop="stop" if mode == "regenerate_complete" else "aborted")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    if mode == "manual":
        retried = await coord.retry_user_message(store.runs[run].user_message_id)
    else:
        retried = await coord.regenerate(store.runs[run].assistant_message_id)
    assert retried and retried != run
    assert calls == [["image"]]
    assert kernel.sent_images[-1] == IMAGES
    intent = next(i for i in store.intents.values() if i.run_id == retried)
    assert intent.attachment_ids == ("image",)
    await finish(kernel, coord)


async def test_missing_attachment_refuses_text_only_retry() -> None:
    kernel, store = FakeKernel(), InMemoryStore()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=context)
    run = await coord.send("picture", attachment_ids=["deleted-image"], images=IMAGES)
    await finish(kernel, coord)
    coord.attachment_builder = lambda ids: AttachmentPayload()
    assert await coord.retry_user_message(store.runs[run].user_message_id) is None
    assert len(kernel.sent) == 1


async def test_busy_covers_route_preflight() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowKernel(FakeKernel):
        async def set_model(self, provider, model_id):
            entered.set()
            await release.wait()
            await super().set_model(provider, model_id)

    kernel = SlowKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(), context=context)
    task = asyncio.create_task(coord.send("first"))
    await entered.wait()
    try:
        assert coord.busy
        assert await coord.send("second") is None
        with pytest.raises(RuntimeError, match="正在处理"), coord.runtime_transition():
            pytest.fail("must not switch models while preflighting")
    finally:
        release.set()
        await task
    assert kernel.sent == ["first"]
    await finish(kernel, coord)


async def test_transport_uses_frozen_intent_and_observed_provider() -> None:
    kernel, store = FakeKernel(), InMemoryStore()
    selected = context()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: selected)
    await coord.send("test")
    kernel.observe(
        {
            "kind": "provider.request",
            "provider": "observed-relay",
            "model": "runtime-model",
            "payload": {"model": "wire-model"},
        }
    )
    kernel.observe({"kind": "provider.response", "status": 200})
    selected = replace(context(), endpoint_id="changed", app_params={"new_setting": 1})
    kernel.emit(
        "message.end",
        {
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "invalid stream body",
            }
        },
    )
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    transport = next(iter(store.transports.values()))
    run = next(iter(store.runs.values()))
    assert run.status is RunStatus.FAILED
    assert run.error == "invalid stream body"
    assert transport.response_status == 200
    assert transport.model_id == "wire-model"
    assert transport.provider == "observed-relay"
    assert "max_tokens" in transport.param_diff["only_in_intent"]
    assert "new_setting" not in transport.param_diff["only_in_intent"]


async def test_unknown_provider_is_not_filled_from_ui() -> None:
    kernel, store = FakeKernel(), InMemoryStore()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=context)
    await coord.send("test")
    kernel.observe({"kind": "provider.request", "payload": {"model": "flash-remote"}})
    await finish(kernel, coord)
    transport = next(iter(store.transports.values()))
    assert transport.provider is None
    assert transport.url is None


async def test_abort_during_route_preflight_never_sends_later() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowKernel(FakeKernel):
        async def set_model(self, provider, model_id):
            entered.set()
            await release.wait()
            await super().set_model(provider, model_id)

    kernel, store = SlowKernel(), InMemoryStore()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=context)
    task = asyncio.create_task(coord.send("cancel me"))
    await entered.wait()
    await coord.abort()
    assert coord.busy  # 预检尚未返回，不能让下一轮改同一个内核。
    release.set()
    run = await task
    await coord.wait_idle()
    assert not kernel.sent
    assert store.runs[run].status is RunStatus.ABORTED
    assert not coord.busy
