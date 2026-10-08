"""Retry context rollback must preserve history, attachments and failure boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.retry import RetryPolicy
from limbowave.domain.run import RunStatus
from limbowave.domain.runtime_state import RuntimeRestoreResult
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel
from tests.unit.test_conversation_process import process_stack as process_stack


class RetryKernel(TreeKernel):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.attachment_contexts = []
        self.fail_restore = False
        self.restore_entered = asyncio.Event()
        self.restore_release = None

    async def new_session(self):
        self.path.clear()

    async def restore_runtime_state(self, snapshot):
        self.restore_entered.set()
        if self.restore_release is not None:
            await self.restore_release.wait()
        if self.fail_restore:
            self.path.clear()  # failure can happen after partially changing runtime
            return RuntimeRestoreResult(success=False, error="restore timeout")
        return await super().restore_runtime_state(snapshot)

    async def set_attachment_context(self, context):
        self.attachment_contexts.append(deepcopy(context))
        await super().set_attachment_context(context)

    async def send_message(self, text, *, images=None):
        await super().send_message(text, images=images)
        self.requests.append(deepcopy(self.path))

    def fail(self, partial=""):
        self._append("assistant", partial)
        self.emit("message.start", {"message": {"role": "assistant"}})
        if partial:
            self.emit("message.update", {"assistantMessageEvent": {
                "type": "text_delta", "delta": partial,
            }})
        self.emit("message.end", {"message": {
            "role": "assistant", "content": [{"type": "text", "text": partial}],
            "stopReason": "error", "errorMessage": "ECONNRESET",
        }})
        self.emit("run.settled", {})


def _setup(automatic=False):
    store = InMemoryStore()
    kernel = RetryKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: RunContext(
        logical_model_id="fake", endpoint_id="fake", routing_reason="test", supports_images=True,
        retry_policy=RetryPolicy(max_attempts=3 if automatic else 1, base_delay_ms=0),
    ))
    return kernel, store, coord


async def _finish(kernel, coord, text="ok"):
    kernel.say(text)
    kernel.emit("run.settled", {})
    await coord.wait_idle()


def _texts(entries):
    return [e["message"]["content"][0]["text"] for e in entries if e["type"] == "message"]


@pytest.mark.parametrize("automatic", [False, True])
async def test_retry_removes_failed_output_but_not_independent_identical_send(automatic):
    kernel, store, coord = _setup(automatic)
    await coord.send("same")
    await _finish(kernel, coord, "earlier answer")
    first = run = await coord.send("same")
    for _ in range(2):
        kernel.fail("" if automatic else "discard this partial answer")
        await coord.wait_idle()
        if not automatic:
            run = await coord.retry_user_message(store.runs[run].user_message_id)
            assert run is not None
        assert _texts(kernel.requests[-1]) == ["same", "earlier answer", "same"]
    await _finish(kernel, coord)
    if not automatic:
        assert store.runs[first].status is RunStatus.FAILED
        assert any(m.content == "discard this partial answer" for m in store.messages.values())
    assert await coord.switch_conversation(coord.conversation_id, coord.branch_id)
    await coord.send("follow up")
    assert _texts(kernel.requests[-1]) == ["same", "earlier answer", "same", "ok", "follow up"]
    await _finish(kernel, coord)


async def test_automatic_retry_preserves_composed_prompt_images_and_rebinds_context():
    kernel, store, coord = _setup(automatic=True)
    images = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
    run = await coord.send("read", document_note="document file-1, 10 lines", images=images)
    original_prompt = kernel.sent[-1]
    assert "file-1" in original_prompt
    for _ in range(2):
        kernel.fail()
        await coord.wait_idle()
        assert _texts(kernel.requests[-1]) == [original_prompt]
    assert kernel.sent == [original_prompt] * 3
    assert kernel.sent_images == [images] * 3
    assert len(kernel.attachment_contexts) == 3
    assert all(c["run_id"] == run for c in kernel.attachment_contexts)
    await _finish(kernel, coord)
    assert store.runs[run].status is RunStatus.COMPLETED


@pytest.mark.parametrize("automatic", [False, True])
async def test_restore_failure_never_sends_again_or_retries_restore_timeout(automatic):
    kernel, store, coord = _setup(automatic)
    await coord.send("history")
    await _finish(kernel, coord)
    run = await coord.send("retry me")
    kernel.fail_restore = True
    kernel.fail()
    await coord.wait_idle()
    if not automatic:
        assert await coord.retry_user_message(store.runs[run].user_message_id) is None
    assert kernel.sent == ["history", "retry me"]
    assert store.runs[run].status is RunStatus.FAILED
    assert await coord.send("must not send into unknown context") is None


@pytest.mark.parametrize("automatic", [False, True])
async def test_abort_during_restore_does_not_send_retry(automatic):
    kernel, store, coord = _setup(automatic)
    await coord.send("history")
    await _finish(kernel, coord)
    run = await coord.send("retry me")
    kernel.restore_release = asyncio.Event()
    kernel.fail()
    if automatic:
        task = asyncio.create_task(coord.wait_idle())
    else:
        await coord.wait_idle()
        task = asyncio.create_task(coord.retry_user_message(store.runs[run].user_message_id))
    await asyncio.wait_for(kernel.restore_entered.wait(), 2)
    await coord.abort()
    kernel.restore_release.set()
    await asyncio.wait_for(task, 2)
    assert kernel.sent == ["history", "retry me"]
    await coord.wait_idle()


async def test_missing_history_mirrors_never_downgrades_retry_to_empty_context():
    kernel, store, coord = _setup()
    await coord.send("important history")
    await _finish(kernel, coord)
    run = await coord.send("retry me")
    kernel.fail()
    await coord.wait_idle()
    store.mirrors.clear()
    assert await coord.retry_user_message(store.runs[run].user_message_id) is None
    assert kernel.sent == ["important history", "retry me"]


async def test_stale_retry_cannot_truncate_a_later_turn():
    kernel, store, coord = _setup()
    old = await coord.send("old")
    kernel.fail()
    await coord.wait_idle()
    await coord.send("new")
    await _finish(kernel, coord)
    before = deepcopy(kernel.path)
    assert await coord.retry_user_message(store.runs[old].user_message_id) is None
    assert kernel.path == before


async def test_retry_retains_accepted_compression_and_uncompressed_tail():
    from tests.unit.test_compression_runtime_regression import setup_chat

    factory, kernel, coord, _, version = await setup_chat()
    assert await coord.apply_compression(version.id)
    await coord.send("keep tail")
    await _finish(kernel, coord, "tail answer")
    run = await coord.send("retry me")
    kernel.say("discard partial", stop="aborted")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        source = uow.runs.get(run).user_message_id
    assert await coord.retry_user_message(source) is not None
    assert _texts(kernel.path) == [
        "old question", "old answer", "keep tail", "tail answer", "retry me",
    ]
    assert [e["summary"] for e in kernel.path if e["type"] == "compaction"] == ["accepted summary"]
    await _finish(kernel, coord)


@pytest.mark.parametrize("automatic", [False, True])
async def test_real_storage_worker_prepares_retry_context_off_loop(
    process_stack, monkeypatch, automatic,
):
    coord, kernel, factory, _, _ = process_stack
    coord._context = lambda: RunContext(
        logical_model_id="fake", endpoint_id="fake", routing_reason="test",
        retry_policy=RetryPolicy(max_attempts=2 if automatic else 1, base_delay_ms=0),
    )
    await coord.send("history")
    await _finish(kernel, coord)
    run = await coord.send("retry me")

    def forbidden(*args, **kwargs):
        raise AssertionError("retry storage work ran on the main loop")

    monkeypatch.setattr(coord, "_prepare_retry", forbidden)
    kernel.emit("message.end", {"message": {
        "role": "assistant", "content": [], "stopReason": "error", "errorMessage": "ECONNRESET",
    }})
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    if not automatic:
        with factory() as uow:
            source = uow.runs.get(run).user_message_id
        assert await coord.retry_user_message(source) is not None
    assert _texts(kernel.path) == ["history", "ok", "retry me"]
    await _finish(kernel, coord)
