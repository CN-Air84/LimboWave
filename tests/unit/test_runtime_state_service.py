"""RuntimeStateService 验收：从持久化镜像重建可移植快照。

这段逻辑原本在集成测试里重复出现（test_recovery_e2e / test_persistence_recovery_e2e），
提升到应用层后由这组单元测试锁住：
- 同一 entry_id 的重复捕获去重（取首次）；
- 按 captured_at 升序；
- 缺失 id/type 字段用镜像元数据补齐；
- 无镜像返回 None；
- 指纹按语义内容计算（不比位置）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.services.runtime_state_service import RuntimeStateService
from limbowave.domain.runtime_mirror import RuntimeEntryMirror
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def _mirror(
    id: str,
    entry_id: str,
    captured_at: datetime,
    entry_type: str = "message",
    payload: dict | None = None,
) -> RuntimeEntryMirror:
    return RuntimeEntryMirror(
        id=id,
        conversation_id="c1",
        entry_id=entry_id,
        entry_type=entry_type,
        captured_at=captured_at,
        payload=payload or {},
    )


def test_build_snapshot_dedups_and_orders(store: InMemoryStore) -> None:
    with in_memory_uow_factory(store)() as uow:
        # 乱序 + 重复捕获
        uow.runtime.add_many(
            [
                _mirror("m3", "e2", T0 + timedelta(seconds=2)),
                _mirror("m1", "e1", T0),
                _mirror("m2", "e1", T0 + timedelta(seconds=1)),  # 重复捕获 e1
            ]
        )
        uow.commit()

    snapshot = RuntimeStateService(in_memory_uow_factory(store)).build_snapshot("c1")
    assert snapshot is not None
    assert [e["id"] for e in snapshot.entries] == ["e1", "e2"]  # 去重 + 升序
    assert snapshot.leaf_entry_id == "e2"
    assert len(snapshot.fingerprint) == 2


def test_missing_fields_filled_from_mirror_metadata(store: InMemoryStore) -> None:
    with in_memory_uow_factory(store)() as uow:
        uow.runtime.add_many([_mirror("m1", "e1", T0, entry_type="message", payload={"custom": 1})])
        uow.commit()

    snapshot = RuntimeStateService(in_memory_uow_factory(store)).build_snapshot("c1")
    assert snapshot is not None
    entry = snapshot.entries[0]
    assert entry["id"] == "e1"  # 从镜像元数据补齐
    assert entry["type"] == "message"
    assert entry["custom"] == 1  # 原 payload 字段保留


def test_no_mirrors_returns_none(store: InMemoryStore) -> None:
    service = RuntimeStateService(in_memory_uow_factory(store))
    assert service.build_snapshot("empty") is None


def test_only_own_conversation(store: InMemoryStore) -> None:
    with in_memory_uow_factory(store)() as uow:
        uow.runtime.add_many(
            [
                _mirror("m1", "e1", T0),
                RuntimeEntryMirror(
                    id="m2",
                    conversation_id="c2",
                    entry_id="e9",
                    entry_type="message",
                    captured_at=T0,
                    payload={},
                ),
            ]
        )
        uow.commit()

    snapshot = RuntimeStateService(in_memory_uow_factory(store)).build_snapshot("c1")
    assert snapshot is not None
    assert [e["id"] for e in snapshot.entries] == ["e1"]  # c2 的不混入


@pytest.mark.parametrize("branch", [False, True])
def test_deleted_conversation_prefix_is_quarantined_without_modifying_store(store, branch):
    """同文旧消息曾被错误关联到新 message_id，边界必须看原始 entry 时间而非关联。"""
    from limbowave.domain.conversation import Conversation

    specs = [
        ("old-user", None, -3600, "message", {"role": "user", "content": "同一句开场"}),
        ("old-reply", "old-user", -3590, "message", {"role": "assistant", "content": "截图"}),
        ("new-user", "old-reply", 1, "message", {"role": "user", "content": "同一句开场"}),
        ("tool", "new-user", 2, "message",
         {"role": "assistant", "content": [{"type": "toolCall", "id": "call"}]}),
        ("result", "tool", 3, "message",
         {"role": "toolResult", "toolCallId": "call", "content": []}),
        ("summary", "result", 4, "compaction", {}),
        ("new-reply", "summary", 5, "message", {"role": "assistant", "content": "新回复"}),
    ]
    factory = in_memory_uow_factory(store)
    with factory() as uow:
        uow.conversations.add(Conversation("c1", "new", T0))
        uow.runtime.add_many([
            RuntimeEntryMirror(
                id=f"m{i}", conversation_id="c1", entry_id=eid,
                entry_type=kind, captured_at=T0 + timedelta(seconds=10 + i),
                parent_entry_id=parent,
                message_id="same-message" if eid.endswith("user") else None,
                payload={
                    "id": eid, "type": kind, "parentId": parent,
                    "timestamp": (T0 + timedelta(seconds=offset)).isoformat(),
                    "message": message,
                    **({"summary": "可能含旧截图", "firstKeptEntryId": "old-user"}
                       if kind == "compaction" else {}),
                },
            ) for i, (eid, parent, offset, kind, message) in enumerate(specs)
        ])
        uow.commit()
    service = RuntimeStateService(factory)
    snapshot = (service.build_branch_snapshot("c1", "new-reply") if branch
                else service.build_snapshot("c1"))
    assert snapshot is not None
    assert [e["id"] for e in snapshot.entries] == ["new-user", "tool", "result", "new-reply"]
    assert snapshot.entries[0]["parentId"] is None
    assert snapshot.entries[-1]["parentId"] == "result"
    assert snapshot.leaf_entry_id == "new-reply"
    assert len(snapshot.fingerprint) == 4
    with factory() as uow:
        original = uow.runtime.list_for_conversation("c1")
        assert len(original) == len(specs)
        assert next(m for m in original if m.entry_id == "new-user").parent_entry_id == "old-reply"


def test_timestamp_precision_does_not_remove_legitimate_first_message():
    from limbowave.application.services.runtime_state_service import isolate_conversation_entries
    from limbowave.domain.conversation import Conversation

    entries = [{"id": "u", "type": "message", "timestamp": T0.isoformat(),
                "message": {"role": "user", "content": "same millisecond"}}]
    conv = Conversation("c", "c", T0 + timedelta(microseconds=900))
    assert isolate_conversation_entries(entries, conv) == entries


def test_known_pollution_without_a_safe_user_boundary_is_not_restored():
    from limbowave.application.services.runtime_state_service import isolate_conversation_entries
    from limbowave.domain.conversation import Conversation

    entries = [
        {"id": "old", "timestamp": (T0 - timedelta(hours=1)).isoformat(),
         "message": {"role": "user", "content": "old"}},
        {"id": "unknown", "parentId": "old", "message": {"role": "user", "content": "?"}},
    ]
    assert isolate_conversation_entries(entries, Conversation("c", "c", T0)) == []


def test_branch_cycle_is_rejected_instead_of_hanging(store):
    factory = in_memory_uow_factory(store)
    with factory() as uow:
        uow.runtime.add_many([
            RuntimeEntryMirror(id="m", conversation_id="c1", entry_id="e",
                               entry_type="message", captured_at=T0, parent_entry_id="e")
        ])
        uow.commit()
    with pytest.raises(ValueError, match="循环"):
        RuntimeStateService(factory).build_branch_snapshot("c1", "e")
