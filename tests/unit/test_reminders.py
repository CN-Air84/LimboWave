from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.services.reminder_service import ReminderService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.reminder import ReminderContext, ReminderStatus
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

NOW = datetime(2026, 10, 25, 14, 0, tzinfo=UTC)
SCOPE = ReminderContext("c1", "b1")


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    factory = (sqlite_uow_factory(tmp_path / "reminders.db", vault_key)
               if request.param == "sqlite" else in_memory_uow_factory())
    with factory() as uow:
        for c, b in [("c1", "b1"), ("c2", "b2")]:
            uow.conversations.add(Conversation(c, c, NOW))
            uow.branches.add(Branch(b, c, NOW))
        uow.commit()
    yield factory
    if hasattr(factory, "close"):
        factory.close()


def test_create_list_cancel_and_owner_boundary(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(SCOPE, target_time="2026-10-25T22:30:00+08:00", reason="喝水")
    assert item.target_time == NOW + timedelta(minutes=30)
    assert item.target_time.isoformat().endswith("+08:00")
    assert service.list_reminders("c1") == [item]
    assert service.list_reminders("c2") == []
    with pytest.raises(ValueError, match="不存在"):
        service.cancel_reminder("c2", item.id)
    assert service.cancel_reminder("c1", item.id).status is ReminderStatus.CANCELLED
    assert service.list_reminders("c1") == []
    assert service.cancel_reminder("c1", item.id).status is ReminderStatus.CANCELLED
    assert service.deliver_due(now=NOW + timedelta(hours=1)) == []


@pytest.mark.parametrize("kwargs", [
    {"target_time": "2026-10-25T23:00:00"},
    {"target_time": "tonight"},
    {"target_time": "2026-10-25T13:59:00Z"},
    {"target_time": "2026-10-25T14:00:00Z"},
    {"delay_seconds": 0}, {"delay_seconds": -1}, {"delay_seconds": True},
    {"delay_seconds": float("inf")}, {"delay_seconds": float("nan")},
    {"delay_seconds": 10**50}, {},
    {"target_time": "2026-10-25T15:00:00Z", "delay_seconds": 60},
])
def test_invalid_deadlines(factory, kwargs):
    with pytest.raises(ValueError):
        ReminderService(factory, clock=lambda: NOW).set_reminder(SCOPE, reason="test", **kwargs)


@pytest.mark.parametrize("reason", ["", "  ", "x" * 2001])
def test_invalid_reason(factory, reason):
    with pytest.raises(ValueError):
        ReminderService(factory, clock=lambda: NOW).set_reminder(
            SCOPE, reason=reason, delay_seconds=60,
        )


def test_delivers_once_to_original_branch_after_restart(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(SCOPE, reason="检查编译结果", delay_seconds=7200)
    assert service.deliver_due(now=NOW) == []
    restarted = ReminderService(factory, clock=lambda: NOW + timedelta(hours=3))
    messages = restarted.deliver_due()
    assert len(messages) == 1
    message = messages[0]
    assert (message.conversation_id, message.branch_id) == ("c1", "b1")
    assert "系统定时提醒" in message.content and "检查编译结果" in message.content
    assert "补发" in message.content
    assert message.run_id is None
    assert restarted.deliver_due() == []
    assert restarted.list_reminders("c1", status="all")[0].status is ReminderStatus.DELIVERED
    with factory() as uow:
        assert uow.messages.get(message.id) == message
    with pytest.raises(ValueError, match="已送达"):
        restarted.cancel_reminder("c1", item.id)


def test_scope_validation_and_deleted_conversation(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    with pytest.raises(ValueError):
        service.set_reminder(ReminderContext("c1", "b2"), reason="test", delay_seconds=60)
    service.set_reminder(SCOPE, reason="test", delay_seconds=60)
    with factory() as uow:
        uow.conversations.delete("c1")
        uow.commit()
    assert service.deliver_due(now=NOW + timedelta(hours=1)) == []
    assert service.list_reminders("c1", status="all") == []


def test_delivery_rollback(factory, monkeypatch):
    service = ReminderService(factory, clock=lambda: NOW)
    service.set_reminder(SCOPE, reason="test", delay_seconds=60)
    with factory() as uow:
        message_type = type(uow.messages)
    original = message_type.add
    with monkeypatch.context() as patch:
        patch.setattr(message_type, "add", lambda *_: (_ for _ in ()).throw(RuntimeError("disk")))
        with pytest.raises(RuntimeError):
            service.deliver_due(now=NOW + timedelta(minutes=2))
    assert message_type.add is original
    assert len(service.list_reminders("c1")) == 1
    assert len(service.deliver_due(now=NOW + timedelta(minutes=2))) == 1


def test_sqlite_encryption_and_concurrent_claim(tmp_path, vault_key):
    path = tmp_path / "db.sqlite"
    factory = sqlite_uow_factory(path, vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation("c1", "title", NOW))
        uow.branches.add(Branch("b1", "c1", NOW))
        uow.commit()
    first = ReminderService(factory, clock=lambda: NOW)
    first.set_reminder(SCOPE, reason="private-reminder-canary", delay_seconds=1)
    other_factory = sqlite_uow_factory(path, vault_key)
    other = ReminderService(other_factory)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(
            lambda service: service.deliver_due(now=NOW + timedelta(seconds=2)), [first, other],
        ))
    assert sum(map(len, results)) == 1
    with factory() as uow:
        assert len(uow.messages.list_for_branch("b1")) == 1
    other_factory.close()
    factory.close()
    assert b"private-reminder-canary" not in path.read_bytes()

def test_quota_and_listing_pagination(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    for index in range(100):
        service.set_reminder(SCOPE, reason=str(index), delay_seconds=60 + index)
    with pytest.raises(ValueError, match="100"):
        service.set_reminder(SCOPE, reason="overflow", delay_seconds=3600)
    first = service.list_reminders("c1", limit=50)
    second = service.list_reminders("c1", limit=50, offset=50)
    assert len(first) == len(second) == 50
    assert {r.id for r in first}.isdisjoint(r.id for r in second)
    assert service.list_reminders("c1", offset=100) == []
    service.cancel_reminder("c1", first[0].id)
    assert service.set_reminder(SCOPE, reason="slot available", delay_seconds=3600)


def test_deleted_branch_cascades_reminders(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    service.set_reminder(SCOPE, reason="test", delay_seconds=60)
    with factory() as uow:
        uow.branches.delete("b1")
        uow.commit()
    assert service.list_reminders("c1", status="all") == []
    assert service.deliver_due(now=NOW + timedelta(hours=1)) == []


def test_cancel_and_delivery_race_has_one_consistent_outcome(tmp_path, vault_key):
    from threading import Barrier

    factory = sqlite_uow_factory(tmp_path / "race.db", vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation("c1", "title", NOW))
        uow.branches.add(Branch("b1", "c1", NOW))
        uow.commit()
    creator = ReminderService(factory, clock=lambda: NOW)
    item = creator.set_reminder(SCOPE, reason="test", delay_seconds=1)
    barrier = Barrier(2)
    def cancel():
        barrier.wait(timeout=2)
        try:
            return ReminderService(factory).cancel_reminder("c1", item.id)
        except ValueError:
            return None
    def deliver():
        barrier.wait(timeout=2)
        return ReminderService(factory).deliver_due(now=NOW + timedelta(seconds=2))
    try:
        with ThreadPoolExecutor(2) as pool:
            cancellation = pool.submit(cancel)
            delivery = pool.submit(deliver)
            result, messages = cancellation.result(), delivery.result()
        with factory() as uow:
            stored = uow.reminders.get(item.id)
            history = uow.messages.list_for_branch("b1")
        if result is not None:
            assert stored.status is ReminderStatus.CANCELLED
            assert history == messages == []
        else:
            assert stored.status is ReminderStatus.DELIVERED
            assert len(history) == len(messages) == 1
    finally:
        factory.close()


def test_v18_upgrade_adds_reminders_without_rewriting_existing_data(tmp_path, vault_key):
    import sqlite3

    from limbowave.infrastructure.database.migrations import current_version, migrate

    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(path)
    try:
        migrate(conn, target=18)
        conn.execute("INSERT INTO conversations(id, title_enc, created_at) VALUES (?, ?, ?)",
                     ("c1", vault_key.encrypt("private-title"), NOW.isoformat()))
        conn.commit()
        before = conn.execute("SELECT * FROM conversations").fetchall()
        migrate(conn)
        assert current_version(conn) == 19
        assert conn.execute("SELECT * FROM conversations").fetchall() == before
        assert conn.execute("SELECT count(*) FROM reminders").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

@pytest.mark.parametrize("offset, expected", [
    (8, "2026-10-26 13:09:16+08:00"),
    (-7, "2026-10-25 22:09:16-07:00"),
])
def test_local_display_does_not_shift_the_actual_deadline(factory, monkeypatch, offset, expected):
    from datetime import timezone

    from limbowave.application.services import reminder_service as module

    zone = timezone(timedelta(hours=offset))
    monkeypatch.setattr(module, "_local_time", lambda value: value.astimezone(zone))
    now = datetime(2026, 10, 26, 5, 8, 46, tzinfo=UTC)
    due = now + timedelta(seconds=30)
    service = ReminderService(factory, clock=lambda: now)
    item = service.set_reminder(SCOPE, reason="time display", delay_seconds=30)
    data = module.reminder_data(item)
    assert data["target_time"] == expected.replace(" ", "T")
    assert data["timezone_source"] == "device_local_timezone"
    assert datetime.fromisoformat(data["target_time"]) == due
    assert datetime.fromisoformat(data["created_at"]) == now
    assert service.deliver_due(now=due - timedelta(seconds=1)) == []
    messages = service.deliver_due(now=due)
    assert len(messages) == 1
    assert f"计划时间：{expected}（设备本地时间）" in messages[0].content
    assert "\n\n由应用定时器发送" in messages[0].content
    with factory() as uow:
        stored = uow.reminders.get(item.id)
        assert stored.target_time == due
        assert stored.target_time.utcoffset() == timedelta(0)
        assert module.reminder_data(stored)["completed_at"] == due.astimezone(zone).isoformat()
