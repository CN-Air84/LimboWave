"""Persist a memory-editing scope without a model call or synthetic turn."""

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from tests.unit.test_memory_service import memory_stack as memory_stack


async def test_ensure_empty_session_is_idempotent_and_retains_model(memory_stack):
    _service, factory = memory_stack
    coord = RunCoordinator(None, factory, context=lambda: RunContext("model", "ep", "test"))
    assert await coord.ensure_session()
    target = coord.conversation_id, coord.branch_id
    assert await coord.ensure_session()
    assert (coord.conversation_id, coord.branch_id) == target
    with factory() as uow:
        assert uow.conversations.get(target[0]).default_logical_model_id == "model"
        assert uow.branches.get(target[1]).conversation_id == target[0]
        assert uow.messages.list_for_branch(target[1]) == []
        assert uow.runs.list_for_conversation(target[0]) == []
    assert await coord.new_session()
    assert coord.conversation_id is None
    assert await coord.ensure_session()
    assert coord.conversation_id != target[0]


async def test_empty_session_commit_failure_does_not_change_scope(memory_stack, monkeypatch):
    _service, factory = memory_stack
    coord = RunCoordinator(None, factory, context=lambda: RunContext("model", "ep", "test"))
    events = []
    coord.subscribe(events.append)
    original = coord._uow_factory

    def failing_factory():
        uow = original()

        def fail():
            raise OSError("commit failed")

        monkeypatch.setattr(uow, "commit", fail)
        return uow

    coord._uow_factory = failing_factory
    assert not await coord.ensure_session()
    assert coord.conversation_id is None and coord.branch_id is None
    assert any("commit failed" in event.data.get("message", "") for event in events)
    coord._uow_factory = original
    assert await coord.ensure_session()


async def test_empty_session_refuses_invalid_runtime(memory_stack):
    _service, factory = memory_stack
    coord = RunCoordinator(None, factory, context=lambda: RunContext("model", "ep", "test"))
    coord._runtime_valid = False
    assert not await coord.ensure_session()
    assert coord.conversation_id is None
