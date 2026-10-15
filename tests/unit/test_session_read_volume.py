"""读取量回归：只解密目标路径，不为定位分支加载无关正文。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.branch_path import current_branch
from limbowave.application.history_view_payload import load_history_view
from limbowave.application.services.compression_context import uncompressed_snapshot
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.application.services.runtime_state_service import RuntimeStateService
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.runtime_mirror import RuntimeEntryMetadata, RuntimeEntryMirror
from limbowave.domain.runtime_state import RuntimeStateSnapshot, fingerprint_from_entry
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import _resume_context

T0 = datetime(2026, 10, 15, tzinfo=UTC)
TARGET = ["e0u", "e0a", "e1u", "e1t", "e1a"]


def seed(factory):
    with factory() as uow:
        uow.conversations.add(Conversation("c", "synthetic", T0))
        for i, second in ((0, 1), (1, 3), (2, 2)):
            bid, rid = f"b{i}", f"r{i}"
            stamp = T0 + timedelta(seconds=second)
            uow.branches.add(
                Branch(
                    bid,
                    "c",
                    T0,
                    parent_branch_id="b0" if i else None,
                    forked_from_message_id="a0" if i else None,
                    include_fork_message=True,
                )
            )
            for role, mid in ((MessageRole.USER, f"u{i}"), (MessageRole.ASSISTANT, f"a{i}")):
                uow.messages.add(
                    Message(
                        mid,
                        "c",
                        bid,
                        role,
                        mid,
                        stamp + timedelta(microseconds=int(role is MessageRole.ASSISTANT)),
                        run_id=rid,
                    )
                )
            uow.runs.add(
                RunRecord(
                    rid,
                    "c",
                    bid,
                    RunStatus.COMPLETED,
                    stamp,
                    f"u{i}",
                    assistant_message_id=f"a{i}",
                )
            )
            parent = "e0a" if i else None
            for suffix, role, message_id in (
                ("u", "user", f"u{i}"),
                ("t", "toolResult", None),
                ("a", "assistant", f"a{i}"),
            ):
                if not i and suffix == "t":
                    continue
                eid = f"e{i}{suffix}"
                entry = {
                    "id": eid,
                    "parentId": parent,
                    "type": "message",
                    "timestamp": stamp.isoformat(),
                    "message": {"role": role, "content": eid},
                }
                uow.runtime.add_many(
                    [
                        RuntimeEntryMirror(
                            f"m{i}{suffix}",
                            "c",
                            eid,
                            "message",
                            stamp,
                            run_id=rid,
                            parent_entry_id=parent,
                            message_id=message_id,
                            payload=entry,
                        )
                    ]
                )
                parent = eid
        uow.conversations.add(Conversation("other", "other", T0))
        uow.runtime.add_many(
            [
                RuntimeEntryMirror(
                    "other-m",
                    "other",
                    "e1a",
                    "message",
                    T0,
                    payload={"id": "e1a", "message": {"role": "assistant", "content": "other"}},
                )
            ]
        )
        uow.commit()


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    result = (
        sqlite_uow_factory(tmp_path / "data.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory(InMemoryStore())
    )
    seed(result)
    yield result
    if request.param == "sqlite":
        result.close()


def test_activity_and_counts_read_only_metadata(factory, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("branch selection decrypted message bodies")

    with factory() as uow:
        monkeypatch.setattr(type(uow.messages), "list_for_branch", forbidden)
        monkeypatch.setattr(VaultKey, "decrypt", forbidden)
        assert uow.messages.summaries_for_branches("c") == {
            "b0": (2, T0 + timedelta(seconds=1, microseconds=1)),
            "b1": (2, T0 + timedelta(seconds=3, microseconds=1)),
            "b2": (2, T0 + timedelta(seconds=2, microseconds=1)),
        }
        assert current_branch(uow, "c").id == "b1"


def test_branch_summaries_include_staged_additions_and_updates(factory):
    with factory() as uow:
        user = uow.messages.get("u1")
        uow.messages.update(replace(user, content="updated"))
        uow.messages.add(
            Message("extra", "c", "b1", MessageRole.USER, "extra", T0 + timedelta(seconds=7))
        )
        assert uow.messages.summaries_for_branches("c")["b1"] == (3, T0 + timedelta(seconds=7))
    with factory() as uow:
        assert uow.messages.summaries_for_branches("c")["b1"][0] == 2


def test_open_history_only_materializes_selected_branch_and_ancestors(factory, monkeypatch):
    with factory() as uow:
        message_type = type(uow.messages)
    original = message_type.list_for_branch
    reads = []

    def recorded(repo, branch_id):
        reads.append(branch_id)
        return original(repo, branch_id)

    monkeypatch.setattr(message_type, "list_for_branch", recorded)
    loaded = load_history_view(factory, "c", None)
    assert loaded.branch_id == "b1"
    assert [m.id for m in loaded.messages] == ["u0", "a0", "u1", "a1"]
    assert reads == ["b0", "b1"]


def test_runtime_metadata_cannot_expose_payloads(factory, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("metadata read decrypted a payload")

    with factory() as uow:
        monkeypatch.setattr(VaultKey, "decrypt", forbidden)
        metadata = uow.runtime.list_metadata_for_conversation("c")
    assert len(metadata) == 8
    assert all(type(m) is RuntimeEntryMetadata and not hasattr(m, "payload") for m in metadata)


def test_switch_only_decodes_target_path(factory, monkeypatch):
    with factory() as uow:
        runtime_type = type(uow.runtime)
    original = runtime_type.list_for_entries
    reads = []

    def recorded(repo, conversation_id, ids):
        reads.append((conversation_id, list(ids)))
        return original(repo, conversation_id, ids)

    def forbidden(*_args, **_kwargs):
        pytest.fail("switch read all runtime payloads")

    monkeypatch.setattr(runtime_type, "list_for_entries", recorded)
    monkeypatch.setattr(runtime_type, "list_for_conversation", forbidden)
    prepared = RunCoordinator(None, factory, context=_resume_context)._prepare_switch(
        "c", "b1", True
    )
    assert prepared.valid
    assert [e["id"] for e in prepared.snapshot.entries] == TARGET
    assert reads == [("c", TARGET)]


def test_target_entry_reads_are_deduplicated_batched_and_scoped(factory):
    with factory() as uow:
        assert not uow.runtime.list_for_entries("c", [])
        rows = uow.runtime.list_for_entries("c", ["missing"] * 600 + ["e1a", "e1a"])
        assert len(rows) == 1 and rows[0].conversation_id == "c"
        assert rows[0].payload["message"]["content"] == "e1a"
        assert uow.runtime.list_for_entries("missing", ["e1a"]) == []


def test_target_read_ignores_corrupted_sibling_payload(tmp_path, vault_key):
    factory = sqlite_uow_factory(tmp_path / "data.db", vault_key)
    try:
        seed(factory)
        with factory() as uow:
            uow._conn.execute(
                "UPDATE runtime_mirrors SET payload_enc = 'invalid' WHERE entry_id LIKE 'e2%'"
            )
            uow.commit()
        prepared = RunCoordinator(None, factory, context=_resume_context)._prepare_switch(
            "c", "b1", True
        )
        assert [e["id"] for e in prepared.snapshot.entries] == TARGET
        snapshot = RuntimeStateService(factory).build_branch_snapshot("c", "e1a")
        assert snapshot.entries == prepared.snapshot.entries
    finally:
        factory.close()


def test_uncompressed_snapshot_reuses_fingerprints_without_overlay(monkeypatch):
    entries = [{"id": "e", "type": "message", "message": {"role": "user", "content": "hi"}}]
    fingerprints = [fingerprint_from_entry(entries[0], 0)]
    snapshot = RuntimeStateSnapshot("c", entries, "e", fingerprints)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unchanged content was fingerprinted twice")

    monkeypatch.setattr(
        "limbowave.application.services.compression_context.fingerprint_from_entry", forbidden
    )
    restored = uncompressed_snapshot(snapshot)
    assert restored.fingerprint is fingerprints
    assert restored.entries == entries and restored.entries is not entries


@pytest.mark.parametrize("indexed", [False, True])
def test_large_requested_path_is_batched_and_complete(tmp_path, vault_key, indexed):
    factory = sqlite_uow_factory(tmp_path / "large.db", vault_key)
    try:
        with factory() as uow:
            uow.conversations.add(Conversation("c", "large", T0))
            uow.runtime.add_many(
                [
                    RuntimeEntryMirror(
                        f"m{i}",
                        "c",
                        f"e{i}",
                        "message",
                        T0 + timedelta(seconds=i),
                        parent_entry_id=f"e{i - 1}" if i else None,
                        payload={"id": f"e{i}"},
                    )
                    for i in range(601)
                ]
            )
            uow.commit()
        with factory() as uow:
            # Older schema coverage also confirms the data port does not depend on indexes.
            if not indexed:
                uow._conn.execute("DROP INDEX IF EXISTS idx_mirrors_conversation_topology")
            rows = uow.runtime.list_for_entries("c", [f"e{i}" for i in reversed(range(601))])
            assert [m.entry_id for m in rows] == [f"e{i}" for i in range(601)]
    finally:
        factory.close()




@pytest.mark.parametrize("service", ["switch", "snapshot"])
def test_topology_and_payload_share_snapshot_during_concurrent_delete(
    tmp_path, vault_key, monkeypatch, service
):
    factory = sqlite_uow_factory(tmp_path / "concurrent.db", vault_key)
    try:
        seed(factory)
        with factory() as uow:
            runtime_type = type(uow.runtime)
        original = runtime_type.list_for_entries

        def delete_then_read(repo, conversation_id, entry_ids):
            with factory() as writer:
                writer._conn.execute("DELETE FROM runtime_mirrors WHERE entry_id = 'e0u'")
                writer.commit()
            return original(repo, conversation_id, entry_ids)

        monkeypatch.setattr(runtime_type, "list_for_entries", delete_then_read)
        if service == "switch":
            snapshot = (
                RunCoordinator(None, factory, context=_resume_context)
                ._prepare_switch("c", "b1", True)
                .snapshot
            )
        else:
            snapshot = RuntimeStateService(factory).build_branch_snapshot("c", "e1a")
        assert [entry["id"] for entry in snapshot.entries] == TARGET
    finally:
        factory.close()
