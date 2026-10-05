"""Runtime regressions: previews must not mutate chat; acceptance must actually compact."""

import asyncio
from copy import deepcopy

import pytest

from limbowave.application.services.compression_service import CompressionService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.compaction import CompressionStatus
from limbowave.domain.runtime_state import RuntimeRestoreResult
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel, _turn
from tests.unit.test_compression_service import SummarizingKernel, _seed
from tests.unit.test_run_coordinator import _resume_context


class PreviewKernel(SummarizingKernel):
    started = False
    stopped = False

    async def start(self):
        self.started = True

    async def shutdown(self):
        self.stopped = True


class ChatKernel(TreeKernel):
    def __init__(self):
        super().__init__()
        self.preview = PreviewKernel()
        self.fail_restore_once = False

    def create_isolated(self):
        return self.preview

    async def restore_runtime_state(self, snapshot):
        if self.fail_restore_once:
            self.fail_restore_once = False
            self.path = []  # emulate a partial runtime transition before failure
            return RuntimeRestoreResult(success=False, error="test restore failure")
        return await super().restore_runtime_state(snapshot)


async def setup_chat(factory=None):
    if factory is None:
        factory = in_memory_uow_factory(InMemoryStore())
    kernel = ChatKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "old question", "old answer")
    service = CompressionService(factory)
    version = service.create_version(
        coord.conversation_id,
        coord.branch_id,
        tokens_before=42000,
        compression_model_id="test",
        compression_endpoint_id="test",
    )
    service.record_result(version.id, "accepted summary", 8)
    return factory, kernel, coord, service, version


async def test_preview_is_isolated_and_shut_down():
    _, kernel, coord, service, version = await setup_chat()
    before = deepcopy(kernel.path)
    sent = list(kernel.sent)
    assert await service.generate_isolated(version.id, kernel)
    assert kernel.path == before and kernel.sent == sent
    assert kernel.preview.started and kernel.preview.stopped
    assert len(kernel.preview.sent) == 1
    assert service.get_active(coord.branch_id) is None
    service.reject(version.id)
    assert kernel.path == before


async def test_no_isolation_support_never_falls_back_to_chat():
    factory = in_memory_uow_factory(InMemoryStore())
    _seed(factory)
    service = CompressionService(factory)
    kernel = SummarizingKernel()
    version = service.create_version(
        "c1", "b1", tokens_before=10, compression_model_id="test", compression_endpoint_id="test"
    )
    assert not await service.generate_isolated(version.id, kernel)
    assert not kernel.sent
    assert service.get(version.id).status is CompressionStatus.FAILED


async def test_accept_restore_resume_and_rollback_preserve_history():
    factory, kernel, coord, service, version = await setup_chat()
    original = deepcopy(kernel.path)
    branch_id, conversation_id = coord.branch_id, coord.conversation_id
    assert await coord.apply_compression(version.id, edited_summary="edited summary")
    assert service.get_active(branch_id).effective_summary == "edited summary"
    marker = next(e for e in kernel.path if e["type"] == "compaction")
    assert marker["summary"] == "edited summary"
    assert kernel.path[: len(original)] == original
    await _turn(kernel, coord, "new question", "new answer")
    assert await coord.new_session()
    assert await coord.resume(conversation_id, branch_id)
    assert sum(e["type"] == "compaction" for e in kernel.path) == 1
    assert kernel.path[-2]["message"]["content"][0]["text"] == "new answer"
    assert await coord.rollback_compression(branch_id)
    assert service.get_active(branch_id) is None
    assert all(e["type"] != "compaction" for e in kernel.path)
    assert [e["message"]["content"][0]["text"] for e in kernel.path] == [
        "old question",
        "old answer",
        "new question",
        "new answer",
    ]
    with factory() as uow:
        assert len(uow.messages.list_for_branch(branch_id)) == 4


async def test_accept_old_preview_preserves_turns_since_preview():
    _, kernel, coord, _service, version = await setup_chat()
    await _turn(kernel, coord, "keep tail", "keep tail answer")
    assert await coord.apply_compression(version.id)
    marker = kernel.path[-1]
    index = next(i for i, e in enumerate(kernel.path) if e["id"] == marker["firstKeptEntryId"])
    assert [e["message"]["content"][0]["text"] for e in kernel.path[index:-1]] == [
        "keep tail",
        "keep tail answer",
    ]


async def test_failed_apply_restores_chat_and_does_not_activate():
    _, kernel, coord, service, version = await setup_chat()
    original = deepcopy(kernel.path)
    kernel.fail_restore_once = True
    assert not await coord.apply_compression(version.id)
    assert service.get_active(coord.branch_id) is None
    assert kernel.path == original
    assert not coord.busy


async def test_preview_timeout_closes_child_without_mutating_chat():
    _, kernel, _, service, version = await setup_chat()
    original = deepcopy(kernel.path)

    async def never_reply(*args, **kwargs):
        await asyncio.Event().wait()

    kernel.preview.send_message = never_reply
    assert not await service.generate_isolated(version.id, kernel, timeout=0.02)
    assert kernel.preview.stopped and kernel.path == original
    assert service.get(version.id).status is CompressionStatus.FAILED


async def test_busy_and_wrong_branch_cannot_apply():
    _, kernel, coord, service, version = await setup_chat()
    before = deepcopy(kernel.path)
    with coord.runtime_transition():
        assert not await coord.apply_compression(version.id)
    assert await coord.new_session()
    assert not await coord.apply_compression(version.id)
    assert not await coord.rollback_compression(version.branch_id)
    assert service.get_active(version.branch_id) is None
    assert before


async def test_failed_rollback_keeps_active_and_restores_effective_context():
    _, kernel, coord, service, version = await setup_chat()
    assert await coord.apply_compression(version.id)
    before = deepcopy(kernel.path)
    kernel.fail_restore_once = True
    assert not await coord.rollback_compression(coord.branch_id)
    assert service.get_active(coord.branch_id).id == version.id
    assert kernel.path == before


async def test_whitelist_is_preserved_even_when_user_removes_it_from_summary():
    factory, kernel, coord, service, version = await setup_chat()
    with factory() as uow:
        message = uow.messages.list_for_branch(coord.branch_id)[0]
    service.toggle_whitelist(message.id)
    assert await coord.apply_compression(version.id, edited_summary="tiny summary")
    assert "old question" in kernel.path[-1]["summary"]
    assert kernel.path[-1]["summary"].startswith("tiny summary")


async def test_preview_cancellation_closes_only_child():
    _, kernel, _, service, version = await setup_chat()
    entered = asyncio.Event()
    before = deepcopy(kernel.path)

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    kernel.preview.send_message = hang
    task = asyncio.create_task(service.generate_isolated(version.id, kernel))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.preview.stopped and kernel.path == before
    assert service.get(version.id).status is CompressionStatus.FAILED


async def test_partial_summary_without_settlement_is_failure():
    _, kernel, _, service, version = await setup_chat()

    async def partial(*args, **kwargs):
        kernel.preview.emit(
            "message.end",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}},
        )

    kernel.preview.send_message = partial
    assert not await service.generate_isolated(version.id, kernel, timeout=0.02)
    assert service.get(version.id).status is CompressionStatus.FAILED
    assert kernel.preview.stopped


async def test_preview_runtime_exit_is_failure_even_with_text():
    _, kernel, _, service, version = await setup_chat()

    async def exit_early(*args, **kwargs):
        kernel.preview.emit(
            "message.end",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}},
        )
        kernel.preview.emit("runtime.exited", {})

    kernel.preview.send_message = exit_early
    assert not await service.generate_isolated(version.id, kernel)
    assert service.get(version.id).status is CompressionStatus.FAILED


async def test_preview_prompt_uses_frozen_message_range():
    _, kernel, coord, service, version = await setup_chat()
    await _turn(kernel, coord, "future message not covered", "future reply")
    assert await service.generate_isolated(version.id, kernel)
    assert "future message not covered" not in kernel.preview.sent[-1]


async def test_native_post_compaction_unknown_usage_is_safe():
    from types import SimpleNamespace

    from limbowave.infrastructure.pi_adapter import PiKernelAdapter
    from limbowave.infrastructure.pi_rpc import SpawnSpec

    async def request(*args, **kwargs):
        return {
            "data": {"contextUsage": {"tokens": None, "percent": None, "contextWindow": 128000}}
        }

    adapter = PiKernelAdapter(SpawnSpec(argv=["unused"]))
    adapter._rpc = SimpleNamespace(request=request)
    assert await adapter.get_context_usage() is None


async def test_sqlite_active_version_survives_new_coordinator(tmp_path, vault_key):
    from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

    factory = sqlite_uow_factory(tmp_path / "compression.db", vault_key)
    _, _, coord, service, version = await setup_chat(factory)
    assert await coord.apply_compression(version.id)
    kernel = ChatKernel()
    restored = RunCoordinator(kernel, factory, context=_resume_context)
    assert await restored.resume(version.conversation_id, version.branch_id)
    assert kernel.path[-1]["type"] == "compaction"
    assert kernel.path[-1]["summary"] == "accepted summary"
    assert service.get_active(version.branch_id).id == version.id
    assert await restored.rollback_compression(version.branch_id)
    assert service.get_active(version.branch_id) is None


def test_removing_overlay_invalidates_only_post_compaction_usage():
    from limbowave.application.services.compression_context import without_application_compactions

    entries = [
        {
            "id": "old",
            "parentId": None,
            "type": "message",
            "message": {"role": "assistant", "usage": {"totalTokens": 40000}},
        },
        {
            "id": "cmp",
            "parentId": "old",
            "type": "compaction",
            "details": {"limbowaveVersion": "v1"},
        },
        {"id": "new-user", "parentId": "cmp", "type": "message", "message": {"role": "user"}},
        {
            "id": "new",
            "parentId": "new-user",
            "type": "message",
            "message": {"role": "assistant", "usage": {"totalTokens": 500}},
        },
    ]
    result = without_application_compactions(entries)
    assert result[-1]["message"]["usage"]["totalTokens"] == 0
    assert result[0]["message"]["usage"]["totalTokens"] == 40000
    assert entries[-1]["message"]["usage"]["totalTokens"] == 500
    assert result[1]["parentId"] == "old"


async def test_resume_then_continue_never_reuses_marker_with_new_parent():
    _, kernel, coord, _, version = await setup_chat()
    assert await coord.apply_compression(version.id)
    first_marker = kernel.path[-1]["id"]
    await _turn(kernel, coord, "tail one", "reply one")
    assert await coord.resume(version.conversation_id, version.branch_id)
    second_marker = kernel.path[-1]["id"]
    assert second_marker != first_marker
    await _turn(kernel, coord, "tail two", "reply two")
    assert await coord.resume(version.conversation_id, version.branch_id)
    assert await coord.rollback_compression(version.branch_id)
    contents = [e["message"]["content"][0]["text"] for e in kernel.path]
    assert contents == [
        "old question",
        "old answer",
        "tail one",
        "reply one",
        "tail two",
        "reply two",
    ]
