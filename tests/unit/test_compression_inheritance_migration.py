"""Upgrade v12 data without needing the vault key or modifying original records."""

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from limbowave.domain.compaction import CompressionStatus, CompressionVersion
from limbowave.domain.conversation import Branch
from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import SqliteUnitOfWork
from tests.unit.test_compression_service import T0, _seed


@pytest.fixture
def legacy(vault_key):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn, target=12)

    def factory():
        return SqliteUnitOfWork(conn, vault_key)

    _seed(factory)
    with factory() as uow:
        v = CompressionVersion(
            id="parent-cmp",
            conversation_id="c1",
            branch_id="b1",
            created_at=T0 + timedelta(minutes=4),
            status=CompressionStatus.ACCEPTED,
            input_message_ids=("m1", "m2", "m3"),
            tokens_before=100,
            tokens_after=10,
            compression_model_id="test",
            compression_endpoint_id="test",
            generated_summary="secret summary",
        )
        uow.compressions.add(v)
        uow.compressions.set_active("b1", v.id)
        uow.branches.add(
            Branch(
                id="child",
                conversation_id="c1",
                created_at=T0 + timedelta(minutes=10),
                parent_branch_id="b1",
                forked_from_message_id="m3",
                include_fork_message=True,
            )
        )
        uow.branches.add(
            Branch(
                id="grandchild",
                conversation_id="c1",
                created_at=T0 + timedelta(minutes=11),
                parent_branch_id="child",
                forked_from_message_id="m3",
                include_fork_message=True,
            )
        )
        uow.commit()
    yield conn, factory
    conn.close()


def test_upgrade_repairs_missing_copies_preserving_ciphertext_and_originals(legacy):
    conn, factory = legacy
    originals = conn.execute("SELECT * FROM compression_versions").fetchall()
    messages = conn.execute("SELECT * FROM messages").fetchall()
    assert migrate(conn) == 13
    with factory() as uow:
        parent = uow.compressions.get_active("b1")
        child = uow.compressions.get_active("child")
        grandchild = uow.compressions.get_active("grandchild")
        assert child and grandchild
        assert len({parent.id, child.id, grandchild.id}) == 3
        assert child.effective_summary == grandchild.effective_summary == parent.effective_summary
        assert child.input_message_ids == parent.input_message_ids
    assert (
        conn.execute("SELECT * FROM compression_versions WHERE branch_id='b1'").fetchall()
        == originals
    )
    assert conn.execute("SELECT * FROM messages").fetchall() == messages
    assert (
        len({r[0] for r in conn.execute("SELECT generated_summary_enc FROM compression_versions")})
        == 1
    )
    migrate(conn)
    assert conn.execute("SELECT count(*) FROM compression_versions").fetchone()[0] == 3


@pytest.mark.parametrize("case", ["inside", "future", "rollback", "failed-local", "bad-prefix"])
def test_upgrade_does_not_resurrect_invalid_or_explicit_local_decisions(legacy, case):
    conn, factory = legacy
    with factory() as uow:
        parent = uow.compressions.get_active("b1")
        if case == "inside":
            conn.execute("UPDATE branches SET include_fork_message=0 WHERE id='child'")
        elif case == "future":
            conn.execute(
                "UPDATE compression_versions SET created_at=?",
                ((T0 + timedelta(hours=1)).isoformat(),),
            )
        elif case == "bad-prefix":
            uow.compressions.update(replace(parent, input_message_ids=("m2", "m3")))
        else:
            local = replace(
                parent,
                id="local",
                branch_id="child",
                status=CompressionStatus.FAILED
                if case == "failed-local"
                else CompressionStatus.ACCEPTED,
            )
            uow.compressions.add(local)  # Local accepted but inactive means explicit rollback.
        uow.commit()
    migrate(conn)
    with factory() as uow:
        assert uow.compressions.get_active("child") is None
        assert uow.compressions.get_active("grandchild") is None
        assert uow.compressions.get_active("b1") is not None


def test_parent_rollback_is_not_reactivated_by_inheritance(legacy):
    conn, factory = legacy
    with factory() as uow:
        uow.compressions.clear_active("b1")
        uow.commit()
    migrate(conn)
    with factory() as uow:
        assert uow.compressions.get_active("child") is None
        assert len(uow.compressions.list_for_branch("child")) == 1
