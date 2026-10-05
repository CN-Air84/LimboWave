"""Compression is branch-local durable state, including across a genuine restart."""

import pytest

from limbowave.application.history_payload import history_payload
from limbowave.application.services.compression_service import CompressionService
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from tests.unit.test_branch_path import _turn
from tests.unit.test_compression_runtime_regression import ChatKernel, setup_chat
from tests.unit.test_run_coordinator import _resume_context


def markers(factory, branch_id):
    messages = HistoryService(factory).branch_messages(branch_id)
    return [
        marker
        for entry in history_payload(messages, uow_factory=factory, branch_id=branch_id)
        for marker in entry.compressions
    ]


@pytest.mark.parametrize("action", ["fork", "regenerate", "edit"])
async def test_compression_survives_branch_and_fresh_restart(tmp_path, vault_key, action):
    path = tmp_path / "restart.db"
    factory, kernel, coord, service, version = await setup_chat(sqlite_uow_factory(path, vault_key))
    parent, conversation = coord.branch_id, coord.conversation_id
    assert await coord.apply_compression(version.id)
    await _turn(kernel, coord, "tail question", "tail answer")
    messages = HistoryService(factory).branch_messages(parent)
    events = []
    coord.subscribe(events.append)
    if action == "fork":
        assert await coord.fork_message(messages[-1].id)
    elif action == "regenerate":
        assert await coord.regenerate(messages[-1].id)
    else:
        assert await coord.edit_user_message(messages[-2].id, "edited tail")
    child = coord.branch_id
    if action != "fork":
        kernel.say("new tail answer")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
    active = service.get_active(child)
    assert active is not None, "fork dropped the accepted compression"
    assert active.id != version.id and active.branch_id == child
    assert active.effective_summary == "accepted summary"
    assert [e["summary"] for e in kernel.path if e["type"] == "compaction"] == ["accepted summary"]
    assert [m.version.id for m in markers(factory, child) if m.is_active] == [active.id]
    branch_event = next(e for e in events if e.kind == "branched")
    assert any(e.compressions for e in branch_event.data["history"])
    await coord.shutdown()
    factory.close()

    # New services, new SQLite connections, and a new kernel; not a same-process new_session.
    factory = sqlite_uow_factory(path, vault_key)
    kernel = ChatKernel()
    kernel._counter = 100
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    service = CompressionService(factory)
    assert await coord.resume(conversation, child)
    assert [e["summary"] for e in kernel.path if e["type"] == "compaction"] == ["accepted summary"]
    assert service.get_active(child).id == active.id
    assert any(m.is_active for m in markers(factory, child))
    await _turn(kernel, coord, "after restart", "still compressed")
    assert await coord.rollback_compression(child)
    assert not any(e["type"] == "compaction" for e in kernel.path)
    assert service.get_active(parent).id == version.id
    assert markers(factory, child) and not any(m.is_active for m in markers(factory, child))
    await coord.shutdown()
    factory.close()
    factory = sqlite_uow_factory(path, vault_key)
    coord = RunCoordinator(ChatKernel(), factory, context=_resume_context)
    assert await coord.resume(conversation, child)
    assert CompressionService(factory).get_active(child) is None
    assert not any(e["type"] == "compaction" for e in coord.kernel.path)
    await coord.shutdown()
    factory.close()


async def test_edit_inside_compressed_prefix_drops_overlay(tmp_path, vault_key):
    factory, kernel, coord, service, version = await setup_chat(
        sqlite_uow_factory(tmp_path / "inside.db", vault_key)
    )
    await _turn(kernel, coord, "covered second", "covered answer")
    version = service.create_version(
        coord.conversation_id,
        coord.branch_id,
        tokens_before=100,
        compression_model_id="test",
        compression_endpoint_id="test",
    )
    service.record_result(version.id, "includes old second answer", 5)
    assert await coord.apply_compression(version.id)
    await _turn(kernel, coord, "tail", "answer")
    assert await coord.resume(coord.conversation_id, coord.branch_id)
    target = HistoryService(factory).branch_messages(coord.branch_id)[2]
    assert await coord.edit_user_message(target.id, "replacement second")
    kernel.say("replacement answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert service.get_active(coord.branch_id) is None
    assert not markers(factory, coord.branch_id)
    assert not any(e["type"] == "compaction" for e in kernel.path)
    assert await coord.resume(coord.conversation_id, coord.branch_id)
    assert not any(e["type"] == "compaction" for e in kernel.path)
    await coord.shutdown()
    factory.close()


async def test_inherited_anchor_uses_current_parent_and_keeps_independent_decisions():
    factory, kernel, coord, service, version = await setup_chat()
    assert await coord.apply_compression(version.id)
    await _turn(kernel, coord, "tail", "answer")
    parent = coord.branch_id
    target = HistoryService(factory).branch_messages(parent)[-1]
    assert await coord.fork_message(target.id)
    first_child = coord.branch_id
    child_version = service.get_active(first_child)
    assert child_version is not None
    assert await coord.apply_compression(child_version.id, edited_summary="child summary")
    # This message physically belongs to the parent, but the fork source is the current branch.
    assert await coord.regenerate(target.id)
    kernel.say("replacement")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        assert uow.branches.get(coord.branch_id).parent_branch_id == first_child
    assert service.get_active(coord.branch_id).effective_summary == "child summary"
    assert service.get_active(parent).effective_summary == "accepted summary"
    assert await coord.rollback_compression(coord.branch_id)
    assert service.get_active(first_child).effective_summary == "child summary"
    await coord.shutdown()


async def test_compression_branch_state_crosses_storage_process(tmp_path, vault_key):
    from limbowave.infrastructure.conversation_process import (
        ConversationProcess,
        ConversationProcessConfig,
    )

    database = tmp_path / "worker.db"
    factory, kernel, coord, service, version = await setup_chat(
        sqlite_uow_factory(database, vault_key)
    )
    worker = ConversationProcess(
        ConversationProcessConfig(database, tmp_path, vault_key.key_bytes())
    )
    coord.storage_worker = worker
    try:
        assert await coord.apply_compression(version.id)
        await _turn(kernel, coord, "worker tail", "worker answer")
        target = HistoryService(factory).branch_messages(coord.branch_id)[-1]
        assert await coord.regenerate(target.id)
        kernel.say("worker replacement")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        assert service.get_active(coord.branch_id).effective_summary == "accepted summary"
        assert any(m.is_active for m in markers(factory, coord.branch_id))
        assert [e["summary"] for e in kernel.path if e["type"] == "compaction"] == [
            "accepted summary"
        ]
    finally:
        await coord.shutdown()
        await worker.close()
        factory.close()
