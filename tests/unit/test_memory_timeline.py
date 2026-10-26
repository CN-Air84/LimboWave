"""Current-only footprint projection against both repository implementations."""

import json
from dataclasses import asdict

from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.memory import MemoryDocument, MemoryItem, MemoryRunContext
from limbowave.domain.run import RunRecord, RunStatus
from tests.unit.test_memory_service import NOW
from tests.unit.test_memory_service import memory_stack as memory_stack


def put_items(factory, key, items, conversation=None, branch=None):
    with factory() as uow:
        uow.memories.put(
            MemoryDocument(
                key,
                json.dumps([asdict(item) for item in items]),
                conversation,
                branch,
            )
        )
        uow.commit()


def test_timeline_reads_current_only_and_deduplicates_forks(memory_stack):
    service, factory = memory_stack
    item = service.save("留下的想法", "c", "b")
    put_items(factory, "branch:other", [item], "c", "other")
    ghost = MemoryItem("ghost", "已删除的秘密", "user", NOW.isoformat())
    put_items(factory, "snapshot:old", [ghost], "c", "b")
    put_items(factory, "receipt:old", [ghost], "c", "b")
    rows = service.timeline()
    assert len(rows) == 1
    assert rows[0].memory == item
    assert {loc.branch_id for loc in rows[0].locations} == {"b", "other"}
    assert rows[0].scope == "session"
    service.delete(item.id, "c", "b")
    assert len(service.timeline()[0].locations) == 1
    service.delete(item.id, "c", "other")
    assert service.timeline() == []


def test_edits_preserve_first_recorded_time_and_model_origin(memory_stack):
    service, factory = memory_stack
    item = MemoryItem("remembered", "原文", "model:run:call", NOW.isoformat())
    put_items(factory, "branch:b", [item], "c", "b")
    put_items(factory, "branch:other", [item], "c", "other")
    edited = service.save("改过的文字", "c", "b", item_id=item.id)
    assert edited.created_at == item.created_at
    assert edited.source == item.source
    assert edited.updated_at is not None
    assert len(service.timeline()) == 2
    assert service.save("改过的文字", "c", "b", item_id=item.id) == edited


def test_global_promotion_remains_distinct_and_survives_source_deletion(memory_stack):
    service, factory = memory_stack
    original = service.save("一段记忆", "c", "b")
    promoted = service.promote(original.id, "c", "b")
    rows = service.timeline()
    assert len(rows) == 2
    # Creation timestamps can tie; promotion identity must not depend on row order.
    by_id = {row.memory.id: row for row in rows}
    assert set(by_id) == {original.id, promoted.id}
    assert by_id[original.id].memory == original
    promoted_row = by_id[promoted.id]
    assert promoted_row.memory == promoted
    assert promoted_row.scope == "global"
    assert promoted_row.origin is not None
    assert promoted_row.origin.branch_id == "b"
    with factory() as uow:
        uow.conversations.delete("c")
        uow.commit()
    rows = service.timeline()
    assert len(rows) == 1 and rows[0].scope == "global"
    assert rows[0].origin is None


def test_model_origin_links_to_actual_user_message(memory_stack):
    service, factory = memory_stack
    with factory() as uow:
        uow.messages.add(Message("m", "c", "b", MessageRole.USER, "我的想法", NOW))
        uow.runs.add(RunRecord("run", "c", "b", RunStatus.COMPLETED, NOW, "m"))
        uow.commit()
    context = MemoryRunContext("run", "c", "b", "m")
    service.approve(context, "call", "被记住")
    item = service.add_from_model(context, "call", "被记住", lambda _: True)
    row = service.timeline()[0]
    assert row.memory == item
    assert row.source_message_id == "m"
    assert row.written_at == NOW.isoformat()
    assert row.origin.conversation_id == "c"
    assert row.origin.branch_id == "b"


def test_timeline_orders_absolute_instants_and_keeps_unknown_dates(memory_stack):
    service, factory = memory_stack
    rows = [
        MemoryItem("earlier", "earlier", "user", "2026-10-25T12:00:00+08:00"),
        MemoryItem("later", "later", "user", "2026-10-25T05:00:00Z"),
        MemoryItem("unknown", "unknown", "user", "old-data"),
    ]
    put_items(factory, "global", rows)
    assert [r.memory.id for r in service.timeline()] == ["later", "earlier", "unknown"]


def test_old_payloads_remain_readable_and_new_dates_stay_encrypted(memory_stack, tmp_path):
    service, factory = memory_stack
    with factory() as uow:
        uow.memories.put(
            MemoryDocument(
                "global",
                json.dumps(
                    [
                        {
                            "id": "legacy",
                            "content": "private footprint marker",
                            "source": "user",
                            "created_at": NOW.isoformat(),
                        }
                    ]
                ),
            )
        )
        uow.commit()
    assert service.timeline()[0].memory.updated_at is None
    service.save("updated private footprint marker", item_id="legacy")
    db = tmp_path / "memory.db"
    if db.exists():
        for path in tmp_path.glob("memory.db*"):
            assert b"private footprint marker" not in path.read_bytes()
    assert service.timeline()[0].memory.created_at == NOW.isoformat()


def test_repository_current_read_respects_staged_writes(memory_stack):
    _, factory = memory_stack
    with factory() as uow:
        uow.memories.put(MemoryDocument("global", "[]"))
        uow.memories.put(MemoryDocument("branch:b", "[]", "c", "b"))
        uow.memories.put(MemoryDocument("snapshot:any", "[]", "c", "b"))
        assert {doc.id for doc in uow.memories.list_current()} == {"global", "branch:b"}
