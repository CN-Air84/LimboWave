"""The live event and persisted history must share one complete tool/timeline record."""

import pytest

from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.conversation import MessageRole
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel, _resume_context


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    result = (
        sqlite_uow_factory(tmp_path / "timeline.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory()
    )
    yield result
    if hasattr(result, "close"):
        result.close()


async def test_live_tool_event_contains_the_persisted_record(factory):
    kernel = FakeKernel()
    coordinator = RunCoordinator(kernel, factory, context=_resume_context)
    events = []
    coordinator.subscribe(events.append)
    await coordinator.send("read")
    kernel.say("checking", stop="toolUse")
    payload = {"toolCallId": "call-1", "toolName": "read", "args": {"path": "notes.md"}}
    kernel.emit("tool.start", payload)
    started = [e for e in events if e.kind == "tool"][-1].data.get("step")
    assert started is not None, "live events currently omit the audit record"
    assert started.args == {"path": "notes.md"}
    coordinator._live.tool_started_at["call-1"] -= 0.25
    kernel.emit("tool.end", {**payload, "result": {"output": "found notes"}})
    done = [e for e in events if e.kind == "tool"][-1].data["step"]
    assert done.result_summary == "found notes"
    assert done.duration_ms >= 250
    kernel.say("answer")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    with factory() as uow:
        message = next(
            m
            for m in uow.messages.list_for_branch(coordinator.branch_id)
            if m.role is MessageRole.ASSISTANT
        )
    assert message.tool_steps == (done,)


@pytest.mark.parametrize("partial", [False, True])
async def test_history_preserves_each_model_segment(factory, partial):
    kernel = FakeKernel()
    coordinator = RunCoordinator(kernel, factory, context=_resume_context)
    await coordinator.send("read")
    kernel.say("checking", stop="toolUse")
    payload = {"toolCallId": "call-1", "toolName": "read", "args": {"path": "notes.md"}}
    kernel.emit("tool.start", payload)
    kernel.emit("tool.end", {**payload, "result": "found notes"})
    kernel.say("checking again", stop="toolUse")
    # An orphan end must be visible and belong to the current segment too.
    kernel.emit("tool.end", {"toolCallId": "call-2", "toolName": "read", "result": "more"})
    if partial:
        kernel.emit("message.start", {"message": {"role": "assistant"}})
        kernel.emit(
            "message.update",
            {"assistantMessageEvent": {"type": "text_delta", "delta": "partial answer"}},
        )
        await coordinator.mark_interrupted()
    else:
        kernel.say("answer")
        kernel.emit("run.settled", {})
        await coordinator.wait_idle()
    with factory() as uow:
        message = next(
            m
            for m in uow.messages.list_for_branch(coordinator.branch_id)
            if m.role is MessageRole.ASSISTANT
        )
    assert [s.content for s in message.segments] == [
        "checking",
        "checking again",
        "partial answer" if partial else "answer",
    ]
    assert [s.tool_call_ids for s in message.segments] == [("call-1",), ("call-2",), ()]


async def test_late_end_keeps_original_segment_and_duplicate_start_is_idempotent(factory):
    kernel = FakeKernel()
    coordinator = RunCoordinator(kernel, factory, context=_resume_context)
    await coordinator.send("read")
    kernel.say("first", stop="toolUse")
    payload = {"toolCallId": "call-1", "toolName": "read", "args": {"path": "notes.md"}}
    kernel.emit("tool.start", payload)
    kernel.emit("tool.start", payload)
    kernel.say("second")
    kernel.emit("tool.end", {**payload, "isError": True, "result": {"error": "not found"}})
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    with factory() as uow:
        message = next(
            m
            for m in uow.messages.list_for_branch(coordinator.branch_id)
            if m.role is MessageRole.ASSISTANT
        )
    assert len(message.tool_steps) == 1
    assert message.tool_steps[0].error == "not found"
    assert [part.tool_call_ids for part in message.segments] == [("call-1",), ()]


def test_segments_are_encrypted_and_survive_update_and_reopen(tmp_path, vault_key):
    from dataclasses import replace

    from limbowave.domain.conversation import AssistantMessageSegment
    from tests.unit.test_tool_steps import _seed

    path = tmp_path / "encrypted-timeline.db"
    factory = sqlite_uow_factory(path, vault_key)
    message_id = _seed(factory, ())
    parts = (
        AssistantMessageSegment(
            content="SECRET-PROCESS-NARRATION", thinking="SECRET-PROCESS-THINKING"
        ),
        AssistantMessageSegment(content="answer"),
    )
    with factory() as uow:
        message = uow.messages.get(message_id)
        uow.messages.update(replace(message, segments=parts))
        uow.commit()
    factory.close()
    assert b"SECRET-PROCESS" not in path.read_bytes()
    reopened = sqlite_uow_factory(path, vault_key)
    try:
        with reopened() as uow:
            assert uow.messages.get(message_id).segments == parts
    finally:
        reopened.close()


def test_v11_database_migrates_without_inventing_segments(tmp_path, vault_key):
    import sqlite3

    from limbowave.infrastructure.database.migrations import migrate

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        migrate(conn, target=11)
        conn.execute(
            "INSERT INTO messages "
            "(id, conversation_id, branch_id, role, content_enc, created_at, "
            "status, tool_steps_enc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "conversation",
                "branch",
                "assistant",
                vault_key.encrypt("old answer"),
                "2026-10-04T00:00:00+00:00",
                "complete",
                vault_key.encrypt("[]"),
            ),
        )
    factory = sqlite_uow_factory(path, vault_key)
    try:
        with factory() as uow:
            old = uow.messages.get("old")
        assert old.content == "old answer"
        assert old.segments == ()
        assert old.tool_steps == ()
    finally:
        factory.close()
