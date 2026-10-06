"""Transcript attachment lookup must not decrypt the whole request history."""

from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.history_view_payload import load_history_view
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory


@pytest.fixture(params=["memory", "sqlite"])
def attachment_store(request, tmp_path, vault_key):
    factory = (
        sqlite_uow_factory(tmp_path / "history.db", vault_key)
        if request.param == "sqlite" else in_memory_uow_factory()
    )
    now = datetime.now(UTC)
    with factory() as uow:
        for cid in ("chosen", "unrelated"):
            uow.conversations.add(Conversation(cid, cid, now))
            uow.branches.add(Branch(f"{cid}-branch", cid, now))
            for index in range(2):
                mid = f"{cid}-user-{index}"
                uow.messages.add(Message(
                    id=mid, conversation_id=cid, branch_id=f"{cid}-branch",
                    role=MessageRole.USER, content=f"message {index}", created_at=now,
                ))
        for index, cid in enumerate(("chosen", "unrelated", "chosen", "chosen")):
            # The last retry intentionally clears user-0's attachments.
            mid = f"{cid}-user-{index % 2}"
            uow.runs.add(RunRecord(
                f"run-{index}", cid, f"{cid}-branch", RunStatus.COMPLETED, now, mid,
            ))
            uow.snapshots.add_intent(RequestIntentSnapshot(
                id=f"intent-{index}", run_id=f"run-{index}", conversation_id=cid,
                branch_id=f"{cid}-branch", logical_model_id="model", endpoint_id="site",
                routing_reason="test", created_at=now + timedelta(seconds=index),
                message_ids=("older-user", mid), attachment_ids=(f"file-{index}",)
                if index != 2 else (), app_params={"large": "private prompt" * 1000},
            ))
        uow.commit()
    yield factory
    if request.param == "sqlite":
        factory.close()


def test_attachment_projection_matches_latest_intent_without_full_reads(
    attachment_store, monkeypatch,
):
    with attachment_store() as uow:
        expected = {
            intent.message_ids[-1]: intent.attachment_ids
            for intent in uow.snapshots.list_all_intents()
            if intent.conversation_id == "chosen" and intent.message_ids
        }

        def forbidden(*args, **kwargs):
            pytest.fail("attachment lookup must not materialize/decrypt intent payloads")

        monkeypatch.setattr(type(uow.snapshots), "list_all_intents", forbidden)
        if hasattr(type(uow.snapshots), "_to_intent"):
            monkeypatch.setattr(type(uow.snapshots), "_to_intent", forbidden)
        result = uow.snapshots.attachment_ids_for_messages(
            "chosen", ["chosen-user-0", "chosen-user-1", "unrelated-user-1", "missing"]
        )
        assert result == expected
        assert result["chosen-user-0"] == ()
        assert result["chosen-user-1"] == ("file-3",)
        assert uow.snapshots.attachment_ids_for_messages("chosen", []) == {}


def test_attachment_projection_handles_long_histories_and_duplicate_ids(attachment_store):
    with attachment_store() as uow:
        result = uow.snapshots.attachment_ids_for_messages(
            "chosen", [f"missing-{i}" for i in range(1200)]
            + ["chosen-user-1", "chosen-user-0", "chosen-user-1"],
        )
        assert result == {"chosen-user-0": (), "chosen-user-1": ("file-3",)}


def test_history_view_uses_scoped_attachment_projection(attachment_store, monkeypatch):
    with attachment_store() as uow:
        snapshot_type = type(uow.snapshots)

    def forbidden(*args, **kwargs):
        pytest.fail("opening one conversation must not scan every intent")

    monkeypatch.setattr(snapshot_type, "list_all_intents", forbidden)
    payload = load_history_view(attachment_store, "chosen", "chosen-branch")
    assert payload is not None
    assert payload.attachment_ids == {"chosen-user-0": (), "chosen-user-1": ("file-3",)}
