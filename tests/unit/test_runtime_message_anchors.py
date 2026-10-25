"""Runtime anchors survive Pi's image hints and repair existing unlinked prompts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from tests.unit.test_branch_path import TreeKernel, _contents, _turn
from tests.unit.test_fork_message import factory as factory
from tests.unit.test_run_coordinator import _resume_context


class NormalizingKernel(TreeKernel):
    async def send_message(self, text, *, images=None):
        await super().send_message(text, images=images)
        # Pi may add resize/unsupported-image hints after the application sent text.
        message = self.path[-1]["message"]
        message["content"][0]["text"] += "\n\n[Image resized by runtime]"
        message["timestamp"] = int(datetime.now(UTC).timestamp() * 1000)
        self.emit("message.end", {"message": message})


async def test_normalized_prompt_keeps_anchor_and_regenerates_after_reopen(factory):
    kernel = NormalizingKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "看图片", "回答")
    original_branch = coord.branch_id
    history = HistoryService(factory)
    original = history.branch_messages(original_branch)
    with factory() as uow:
        user = next(
            m
            for m in uow.runtime.list_for_conversation(coord.conversation_id)
            if m.entry_id == "e1"
        )
        assert user.message_id == original[0].id
    reopened = RunCoordinator(kernel, factory, context=_resume_context)
    assert await reopened.resume(coord.conversation_id, original_branch)
    assert await reopened.fork_message(original[-1].id)
    assert await reopened.regenerate(original[-1].id)
    kernel.say("新回答")
    kernel.emit("run.settled", {})
    await reopened.wait_idle()
    assert kernel.sent == ["看图片", "看图片"]
    assert _contents(history.branch_messages(reopened.branch_id)) == ["看图片", "新回答"]
    assert history.branch_messages(original_branch) == original


def replace_mirrors(uow, mirrors):
    """Inject a legacy broken row in test storage, bypassing NULL-only repair API."""
    if hasattr(uow, "_store"):
        for mirror in mirrors:
            uow._store.mirrors[mirror.id] = mirror
    else:
        for mirror in mirrors:
            uow._conn.execute("DELETE FROM runtime_mirrors WHERE id = ?", (mirror.id,))
        uow.runtime.add_many(mirrors)


def orphan_user(factory, conversation_id, entry_id, *, timestamp=True, **changes):
    with factory() as uow:
        mirrors = uow.runtime.list_for_conversation(conversation_id)
        mirror = next(m for m in mirrors if m.entry_id == entry_id)
        run = uow.runs.get(mirror.run_id)
        payload = {
            **mirror.payload,
            "message": {
                **mirror.payload["message"],
                "content": [{"type": "text", "text": "runtime-transformed prompt"}],
            },
        }
        if timestamp:
            payload["message"]["timestamp"] = int(run.created_at.timestamp() * 1000)
        replacement = replace(mirror, message_id=None, payload=payload, **changes)
        # Repository updates deliberately only permit NULL -> message links.
        replace_mirrors(uow, [replacement])
        uow.commit()
    return replacement


@pytest.mark.parametrize("action", ["regenerate", "edit"])
async def test_legacy_normalized_prompt_repairs_only_its_run_after_reopen(factory, action):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "同一个问题", "答一")
    await _turn(kernel, coord, "同一个问题", "答二")
    branch = coord.branch_id
    history = HistoryService(factory)
    original = history.branch_messages(branch)
    broken = orphan_user(factory, coord.conversation_id, "e3")
    reopened = RunCoordinator(kernel, factory, context=_resume_context)
    assert await reopened.resume(coord.conversation_id, branch)
    if action == "regenerate":
        result = await reopened.regenerate(original[-1].id)
    else:
        result = await reopened.edit_user_message(original[-2].id, "改后的问题")
    assert result is not None
    assert [e["id"] for e in kernel.path[:2]] == ["e1", "e2"]
    with factory() as uow:
        repaired = next(
            m for m in uow.runtime.list_for_conversation(coord.conversation_id) if m.id == broken.id
        )
        assert repaired.message_id == original[-2].id
    kernel.say("新回答")
    kernel.emit("run.settled", {})
    await reopened.wait_idle()
    assert history.branch_messages(branch) == original


@pytest.mark.parametrize("damage", ["missing_time", "old_time", "wrong_run", "ambiguous"])
async def test_legacy_repair_refuses_unproven_or_ambiguous_user_entry(factory, damage):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "第一问", "答一")
    await _turn(kernel, coord, "第二问", "答二")
    branch = coord.branch_id
    original = HistoryService(factory).branch_messages(branch)
    broken = orphan_user(factory, coord.conversation_id, "e3", timestamp=damage != "missing_time")
    with factory() as uow:
        if damage == "old_time":
            run = uow.runs.get(broken.run_id)
            payload = {
                **broken.payload,
                "message": {
                    **broken.payload["message"],
                    "timestamp": int((run.created_at - timedelta(days=1)).timestamp() * 1000),
                },
            }
            broken = replace(broken, payload=payload)
        elif damage == "wrong_run":
            broken = replace(broken, run_id=original[0].run_id)
        replace_mirrors(uow, [broken])
        if damage == "ambiguous":
            uow.runtime.add_many(
                [
                    replace(
                        broken,
                        id="extra",
                        entry_id="extra",
                        parent_entry_id="e3",
                        payload={**broken.payload, "id": "extra", "parentId": "e3"},
                    )
                ]
            )
        uow.commit()
    assert await coord.regenerate(original[-1].id) is None
    assert coord.branch_id == branch
    assert kernel.sent == ["第一问", "第二问"]


async def test_accepted_timestamp_does_not_link_same_text_foreign_entry(factory):
    kernel = NormalizingKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("same text")
    stamp = kernel.path[-1]["message"]["timestamp"]
    kernel._append("user", "same text")
    kernel.path[-1]["message"]["timestamp"] = stamp - 10000
    kernel.say("answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        users = [
            m
            for m in uow.runtime.list_for_conversation(coord.conversation_id)
            if m.payload.get("message", {}).get("role") == "user"
        ]
        assert {m.entry_id for m in users if m.message_id is not None} == {"e1"}


async def test_duplicate_accepted_timestamp_does_not_guess_an_anchor(factory):
    kernel = NormalizingKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("question")
    stamp = kernel.path[-1]["message"]["timestamp"]
    kernel._append("user", "question")
    kernel.path[-1]["message"]["timestamp"] = stamp
    kernel.say("answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        users = [
            m
            for m in uow.runtime.list_for_conversation(coord.conversation_id)
            if m.payload.get("message", {}).get("role") == "user"
        ]
        assert all(m.message_id is None for m in users)


async def test_timestamp_less_kernel_refuses_duplicate_exact_text_entries(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("same text")
    kernel._append("user", "same text")
    kernel.say("answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        users = [
            m
            for m in uow.runtime.list_for_conversation(coord.conversation_id)
            if m.payload.get("message", {}).get("role") == "user"
        ]
        assert all(m.message_id is None for m in users)
