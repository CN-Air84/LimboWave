import asyncio

import pytest

from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.run_origin import RUN_ORIGIN
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel


@pytest.fixture
async def runtime():
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = FakeKernel()
    coordinator = RunCoordinator(
        kernel, factory, context=lambda: RunContext("fake", "fake", "test")
    )
    session = SessionController(kernel, coordinator)
    facade = RuntimeFacade(session, factory)
    yield facade, session, kernel, store
    if session.busy:
        await session.mark_interrupted()
    await session.wait_idle()
    await facade.close()


def cmd(facade, kind, identity="cmd", **fields):
    return {
        "type": kind,
        "server_epoch": facade.epoch,
        "client_command_id": identity,
        "expected_revision": facade.revision,
        **fields,
    }


async def draft(facade):
    result = await facade.execute("device", cmd(facade, "new_session", "draft"))
    assert result["status"] == "accepted", result
    return {key: result[key] for key in ("conversation_id", "branch_id")}


async def test_real_coordinator_send_dedup_stop_and_origin(runtime):
    facade, session, kernel, store = runtime
    target = await draft(facade)
    command = cmd(facade, "send", text="hello", **target)
    accepted = await facade.execute("device", command)
    assert accepted["status"] == "accepted"
    assert accepted["run_id"] == session.run_id
    assert session.coordinator().event_scope()["run_origin"] == "web"
    assert RUN_ORIGIN.get() == "desktop"
    assert await facade.execute("device", command) == accepted
    assert len(store.messages) == len(store.runs) == len(kernel.sent) == 1
    wrong = await facade.execute("device", cmd(facade, "abort", "wrong", run_id="old"))
    assert wrong["error"]["code"] == "run_mismatch" and not kernel.aborted
    stopped = await facade.execute("device", cmd(facade, "abort", "stop", run_id=session.run_id))
    assert stopped["status"] == "accepted" and kernel.aborted


async def test_revision_target_and_untrusted_fields_are_rejected(runtime):
    facade, _session, kernel, _store = runtime
    target = await draft(facade)
    stale = cmd(facade, "send", "stale", text="bad", **target)
    stale["expected_revision"] = facade.revision + 1
    assert (await facade.execute("device", stale))["error"]["code"] == "revision_mismatch"
    invalid = cmd(
        facade,
        "send",
        "invalid",
        text="bad",
        conversation_id="missing",
        branch_id=target["branch_id"],
    )
    assert (await facade.execute("device", invalid))["error"]["code"] == "target_not_found"
    origin = cmd(facade, "send", "origin", text="bad", run_origin="desktop", **target)
    assert (await facade.execute("device", origin))["status"] == "rejected"
    assert not kernel.sent
    with pytest.raises(RuntimeConflict, match="epoch_mismatch"):
        await facade.execute("device", {**stale, "server_epoch": "old"})


async def test_disconnected_request_keeps_pending_task_and_run(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    entered, release = asyncio.Event(), asyncio.Event()

    async def before():
        entered.set()
        await release.wait()

    session.coordinator().before_prompt = before
    command = cmd(facade, "send", text="hello", **target)
    request = asyncio.create_task(facade.execute("device", command))
    await entered.wait()
    assert facade.receipt("device", "cmd")["status"] == "pending"
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert (await facade.execute("device", command))["status"] == "pending"
    assert await session.send("desktop race") is None
    release.set()
    for _ in range(20):
        if facade.receipt("device", "cmd")["status"] != "pending":
            break
        await asyncio.sleep(0)
    assert facade.receipt("device", "cmd")["status"] == "accepted"
    assert kernel.sent == ["hello"]


async def test_abort_is_not_held_by_startup_reservation(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    entered, release = asyncio.Event(), asyncio.Event()

    async def before():
        entered.set()
        await release.wait()

    session.coordinator().before_prompt = before
    request = asyncio.create_task(
        facade.execute("device", cmd(facade, "send", text="hello", **target))
    )
    await entered.wait()
    result = await asyncio.wait_for(
        facade.execute("device", cmd(facade, "abort", "stop", run_id=session.run_id)), 0.5
    )
    assert result["status"] == "accepted"
    release.set()
    await request
    assert kernel.sent == []


async def test_snapshot_and_replay_do_not_lose_delta_and_late_attach(runtime):
    facade, session, kernel, store = runtime
    target = await draft(facade)
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit(
        "message.update", {"assistantMessageEvent": {"type": "text_delta", "delta": "first"}}
    )
    state = await facade.state()
    assert state["stream"]["text"] == "first"
    kernel.emit(
        "message.update", {"assistantMessageEvent": {"type": "text_delta", "delta": "second"}}
    )
    sub = facade.events.subscribe(after_seq=state["seq"], server_epoch=facade.epoch)
    event = await anext(sub)
    assert event["payload"]["text"] == "second"
    assert event["run_id"] == session.run_id and event["message_id"]
    late = RuntimeFacade(session, in_memory_uow_factory(store))
    assert (await late.state())["stream"]["text"] == "firstsecond"
    await late.close()
    sub.close()


async def test_models_ready_and_failure_receipt_are_sanitized(runtime):
    _facade, session, _kernel, store = runtime
    ready = True

    async def select(identity):
        raise RuntimeError("secret filesystem path")

    other = RuntimeFacade(
        session,
        in_memory_uow_factory(store),
        ready=lambda: ready,
        models_provider=lambda: [{"id": "m", "name": "Model", "api_key": "secret"}],
        select_model=select,
    )
    assert await other.models() == [{"id": "m", "name": "Model"}]
    result = await other.execute("device", cmd(other, "select_model", model_id="m"))
    assert result["status"] == "failed" and "secret" not in str(result)
    ready = False
    assert not (await other.state())["available"]
    with pytest.raises(RuntimeConflict):
        await other.models()
    await other.close()


async def test_permission_callback_uses_trusted_active_origin(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    observed = []

    async def handler(title, detail):
        observed.append(RUN_ORIGIN.get())
        return False

    session.set_permission_handler(handler)
    assert RUN_ORIGIN.get() == "desktop"
    assert not await kernel.permission_handler("tool", "detail")
    assert observed == ["web"] and RUN_ORIGIN.get() == "desktop"


async def test_invalidate_removes_stream_and_stops_new_reads(runtime):
    facade, _session, _kernel, _store = runtime
    sub = facade.events.subscribe()
    facade.invalidate()
    assert (await anext(sub))["kind"] == "runtime_unavailable"
    assert not (await facade.state())["available"]
    with pytest.raises(RuntimeConflict):
        await facade.execute("device", cmd(facade, "new_session"))


async def test_draft_does_not_switch_and_remote_hook_precedes_restore(runtime):
    facade, session, _kernel, _store = runtime
    target = await draft(facade)
    assert session.conversation_id is None and session.branch_id is None
    seen = []
    session.subscribe(lambda event: seen.append((event, session.conversation_id)))
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    hook = next(row for row in seen if row[0].kind == "remote_target_changing")
    assert hook[1] is None
    assert hook[0].data["target_conversation_id"] == target["conversation_id"]
    assert next(row for row in seen if row[0].kind == "user")[1] == target["conversation_id"]


async def test_epoch_rotation_preserves_run_but_isolates_old_pending_receipts(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    entered, release = asyncio.Event(), asyncio.Event()

    async def before():
        entered.set()
        await release.wait()

    session.coordinator().before_prompt = before
    command = cmd(facade, "send", text="hello", **target)
    pending = asyncio.create_task(facade.execute("device", command))
    await entered.wait()
    old_epoch, run_id = facade.epoch, session.run_id
    sub = facade.events.subscribe()
    new_epoch = facade.rotate_epoch()
    assert new_epoch != old_epoch and session.run_id == run_id
    assert (await anext(sub))["kind"] == "resync_required"
    assert facade.receipt("device", "cmd") is None
    with pytest.raises(RuntimeConflict, match="epoch_mismatch"):
        await facade.execute("device", command)
    release.set()
    result = await pending
    assert result["server_epoch"] == old_epoch and result["status"] == "accepted"
    assert facade.receipt("device", "cmd") is None
    assert kernel.sent == ["hello"]


async def test_shared_reservation_blocks_desktop_during_model_selection(runtime):
    _facade, session, kernel, store = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def select(identity):
        entered.set()
        await release.wait()

    other = RuntimeFacade(
        session,
        in_memory_uow_factory(store),
        models_provider=lambda: [{"id": "model"}],
        select_model=select,
    )
    request = asyncio.create_task(
        other.execute("device", cmd(other, "select_model", model_id="model"))
    )
    await entered.wait()
    assert await session.send("racing desktop") is None
    assert not await session.new_session()
    release.set()
    assert (await request)["status"] == "accepted"
    assert not kernel.sent
    await other.close()


async def test_retry_spawned_from_desktop_event_task_keeps_web_origin(runtime):
    from dataclasses import replace

    from limbowave.domain.retry import RetryPolicy

    facade, session, kernel, _store = runtime
    coordinator = session.coordinator()
    context = coordinator._context()
    coordinator._context = lambda: replace(context, retry_policy=RetryPolicy(base_delay_ms=0))
    observed = []
    original_send = kernel.send_message

    async def send(text, **kwargs):
        observed.append(RUN_ORIGIN.get())
        await original_send(text, **kwargs)

    kernel.send_message = send
    target = await draft(facade)
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    assert RUN_ORIGIN.get() == "desktop"
    kernel.emit(
        "message.end",
        {"message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET"}},
    )
    kernel.emit("run.settled", {})
    for _ in range(30):
        if len(observed) == 2:
            break
        await asyncio.sleep(0)
    assert observed == ["web", "web"]


async def test_run_finalization_retains_remote_scope_for_desktop_filter(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    accepted = await facade.execute("device", cmd(facade, "send", text="hello", **target))
    kernel.say("answer")
    kernel.emit("run.settled", {})
    await session.wait_idle()
    scope = session.coordinator().event_scope()
    assert scope["run_origin"] == "web"
    assert scope["run_id"] == accepted["run_id"]
    assert scope["conversation_id"] == target["conversation_id"]
    assert session.coordinator().active_run_id is None
    result = await facade.execute("device", cmd(facade, "abort", "old", run_id=accepted["run_id"]))
    assert result["error"]["code"] == "run_mismatch"
    assert await session.new_session()
    assert session.coordinator().run_origin == "desktop"


async def test_receipt_recovery_after_device_revoke_does_not_cancel_model(runtime):
    facade, session, kernel, _store = runtime
    target = await draft(facade)
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    facade.revoke_device("device")
    assert facade.receipt("device", "cmd") is None
    assert session.busy and not kernel.aborted


async def test_new_facade_epoch_does_not_replay_previous_commands(runtime):
    facade, session, kernel, store = runtime
    target = await draft(facade)
    command = cmd(facade, "send", text="hello", **target)
    await facade.execute("device", command)
    replacement = RuntimeFacade(session, in_memory_uow_factory(store))
    with pytest.raises(RuntimeConflict, match="epoch_mismatch"):
        await replacement.execute("device", command)
    assert kernel.sent == ["hello"]
    await replacement.close()


async def test_missing_native_history_fails_closed_without_scanning(runtime):
    facade, _session, _kernel, _store = runtime
    facade._history = None
    with pytest.raises(RuntimeConflict, match="history_unavailable") as missing:
        await facade.conversations()
    assert missing.value.status == 503
    with pytest.raises(RuntimeConflict, match="invalid_limit"):
        await facade.messages("conversation", "branch", limit=101)


async def test_history_adapter_preserves_page_contract_and_never_switches(runtime):
    facade, session, _kernel, _store = runtime
    import threading

    main_thread = threading.get_ident()
    calls = []

    class NativePages:
        def page_conversations(self, *, limit=50, cursor=None):
            calls.append(("conversations", limit, cursor, threading.get_ident()))
            return {"items": [{"id": "c", "title": "title"}], "next_cursor": "opaque"}

        def page_messages(self, conversation_id, branch_id, *, limit=50, cursor=None):
            calls.append((conversation_id, branch_id, limit, cursor, threading.get_ident()))
            return {"items": [{"id": "m", "content": "message"}], "next_cursor": None}

    facade._history = NativePages()
    assert (await facade.conversations(limit=10))["next_cursor"] == "opaque"
    assert (await facade.messages("c", "b", cursor="opaque"))["items"][0]["id"] == "m"
    assert all(call[-1] != main_thread for call in calls)
    assert session.conversation_id is None


async def test_live_tool_snapshot_is_restorable_without_private_arguments(runtime):
    from limbowave.web.dto import project

    facade, _session, kernel, _store = runtime
    target = await draft(facade)
    await facade.execute("device", cmd(facade, "send", text="hello", **target))
    kernel.say("checking", stop="toolUse")
    kernel.emit(
        "tool.start", {"toolCallId": "call", "toolName": "read", "args": {"path": "SECRET"}}
    )
    snapshot = project(await facade.state(), "state")
    assert snapshot["stream"]["tools"] == [{"tool_id": "call", "name": "read", "status": "running"}]
    assert "SECRET" not in str(snapshot)
    kernel.emit(
        "tool.end",
        {"toolCallId": "call", "toolName": "read", "isError": True, "result": {"error": "SECRET"}},
    )
    assert project(await facade.state(), "state")["stream"]["tools"][0]["status"] == "failed"
