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
        assert current_version(conn) == 20
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


@pytest.mark.parametrize("kind, next_time", [
    ("daily", "2026-10-31T12:30:00+08:00"),
    ("weekdays", "2026-11-02T12:30:00+08:00"),
    ("weekly", "2026-11-06T12:30:00+08:00"),
])
def test_recurring_delivery_advances_and_can_cancel_after_delivery(factory, kind, next_time):
    service = ReminderService(factory, clock=lambda: NOW)
    due = datetime.fromisoformat("2026-10-30T12:30:00+08:00")
    item = service.set_reminder(
        SCOPE, reason="午休一下", target_time=due.isoformat(),
        recurrence=kind, timezone="Asia/Shanghai",
    )
    messages = service.deliver_due(now=due)
    assert len(messages) == 1 and messages[0].run_id is None
    assert "\n\n周期：" in messages[0].content
    assert "\n\n下一次：" in messages[0].content
    assert "\n\n错过多次只补发一次" in messages[0].content
    if kind == "weekly":
        assert "周期：每周五" in messages[0].content
    pending = service.list_reminders("c1")
    assert pending[0].id == item.id and pending[0].completed_at is None
    assert pending[0].target_time == datetime.fromisoformat(next_time)
    assert service.deliver_due(now=due) == []
    following = ReminderService(factory).deliver_due(now=pending[0].target_time)
    assert len(following) == 1 and following[0].id != messages[0].id
    assert service.cancel_reminder("c1", item.id).status is ReminderStatus.CANCELLED
    assert service.deliver_due(now=due + timedelta(days=30)) == []


def test_recurring_long_absence_coalesces_to_one_notice(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(
        SCOPE, reason="休息", target_time="2026-10-26T23:00:00+08:00",
        recurrence="daily", timezone="Asia/Shanghai",
    )
    resumed = datetime.fromisoformat("2026-11-10T23:30:00+08:00")
    assert len(ReminderService(factory).deliver_due(now=resumed)) == 1
    assert service.deliver_due(now=resumed) == []
    assert service.list_reminders("c1")[0].id == item.id
    assert service.list_reminders("c1")[0].target_time == (
        datetime.fromisoformat("2026-11-11T23:00:00+08:00")
    )


def test_source_context_is_captured_not_latest_message_at_delivery(factory):
    from limbowave.domain.conversation import Message, MessageRole

    with factory() as uow:
        uow.messages.add(Message("original-user", "c1", "b1", MessageRole.USER,
                                 "两小时后叫我", NOW - timedelta(seconds=1)))
        uow.commit()
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(SCOPE, reason="回来聊", delay_seconds=7200)
    assert item.source_message_id == "original-user"
    with factory() as uow:
        uow.messages.add(Message("later-user", "c1", "b1", MessageRole.USER,
                                 "下一件事", NOW + timedelta(seconds=1)))
        uow.commit()
    notice = service.deliver_due(now=NOW + timedelta(hours=2))[0]
    assert ReminderService(factory).context_for_notice("c1", "b1", notice.id) == (
        "c1", "b1", "original-user",
    )
    assert service.context_for_notice("c2", "b2", notice.id) is None
    assert service.context_for_notice("c1", "b1", "original-user") is None


def test_notice_without_source_falls_back_to_itself(factory):
    service = ReminderService(factory, clock=lambda: NOW)
    service.set_reminder(SCOPE, reason="提醒", delay_seconds=1)
    notice = service.deliver_due(now=NOW + timedelta(seconds=1))[0]
    assert service.context_for_notice("c1", "b1", notice.id) == ("c1", "b1", notice.id)


@pytest.mark.parametrize("params", [
    {"recurrence": "daily", "timezone": "Asia/Shanghai", "delay_seconds": 60},
    {"recurrence": "daily", "target_time": "2026-10-26T23:00:00+08:00"},
    {"timezone": "Asia/Shanghai", "delay_seconds": 60},
])
def test_invalid_recurrence_service_params(factory, params):
    with pytest.raises(ValueError):
        ReminderService(factory, clock=lambda: NOW).set_reminder(SCOPE, reason="test", **params)



def test_explicit_origin_is_current_turn_even_if_later_user_exists(factory):
    from limbowave.domain.conversation import Message, MessageRole

    with factory() as uow:
        uow.messages.add(Message("turn-origin", "c1", "b1", MessageRole.USER,
                                 "午休提醒", NOW - timedelta(seconds=2)))
        uow.messages.add(Message("later-user", "c1", "b1", MessageRole.USER,
                                 "另一个话题", NOW - timedelta(seconds=1)))
        uow.messages.add(Message("foreign-user", "c2", "b2", MessageRole.USER,
                                 "其他会话", NOW))
        uow.commit()
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(
        ReminderContext("c1", "b1", "turn-origin"), reason="午休一下",
        target_time="2026-10-26T12:30:00+08:00", recurrence="weekdays", timezone="Asia/Shanghai",
    )
    assert item.source_message_id == "turn-origin"
    for now in (item.target_time, item.target_time + timedelta(days=1)):
        notice = service.deliver_due(now=now)[0]
        assert service.context_for_notice("c1", "b1", notice.id) == ("c1", "b1", "turn-origin")
    service.cancel_reminder("c1", item.id)
    assert service.context_for_notice("c1", "b1", notice.id) == ("c1", "b1", "turn-origin")
    for source_id in ("foreign-user", "missing-user"):
        with pytest.raises(ValueError, match="来源"):
            service.set_reminder(ReminderContext("c1", "b1", source_id),
                                 reason="test", delay_seconds=1)


def test_missing_source_falls_back_to_notice(factory, monkeypatch):
    from limbowave.domain.conversation import Message, MessageRole

    with factory() as uow:
        uow.messages.add(Message("origin", "c1", "b1", MessageRole.USER, "test", NOW))
        message_type = type(uow.messages)
        uow.commit()
    service = ReminderService(factory, clock=lambda: NOW)
    service.set_reminder(SCOPE, reason="test", delay_seconds=1)
    notice = service.deliver_due(now=NOW + timedelta(seconds=1))[0]
    monkeypatch.setattr(message_type, "history_position", lambda *_: None)
    assert service.context_for_notice("c1", "b1", notice.id) == ("c1", "b1", notice.id)


def test_recurring_delivery_rollback_does_not_lose_occurrence(factory, monkeypatch):
    service = ReminderService(factory, clock=lambda: NOW)
    item = service.set_reminder(
        SCOPE, reason="test", target_time="2026-10-26T23:00:00+08:00",
        recurrence="daily", timezone="Asia/Shanghai",
    )
    with factory() as uow:
        message_type = type(uow.messages)
    with monkeypatch.context() as patch:
        patch.setattr(message_type, "add", lambda *_: (_ for _ in ()).throw(RuntimeError("disk")))
        with pytest.raises(RuntimeError):
            service.deliver_due(now=item.target_time)
    assert service.list_reminders("c1")[0].target_time == item.target_time
    assert len(service.deliver_due(now=item.target_time)) == 1
    assert service.deliver_due(now=item.target_time) == []


def test_sqlite_recurring_concurrent_claim_and_encrypted_details(tmp_path, vault_key):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "recurring.db"
    factory = sqlite_uow_factory(path, vault_key)
    other_factory = sqlite_uow_factory(path, vault_key)
    try:
        with factory() as uow:
            uow.conversations.add(Conversation("c1", "title", NOW))
            uow.branches.add(Branch("b1", "c1", NOW))
            uow.commit()
        service = ReminderService(factory, clock=lambda: NOW)
        item = service.set_reminder(
            SCOPE, reason="private-recurring-canary", target_time="2026-10-26T23:00:00+08:00",
            recurrence="daily", timezone="Asia/Shanghai",
        )
        barrier = Barrier(2)
        def deliver(factory):
            barrier.wait(timeout=5)
            return ReminderService(factory).deliver_due(now=item.target_time)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(deliver, [factory, other_factory]))
        assert sum(map(len, results)) == 1
        assert service.list_reminders("c1")[0].target_time == item.target_time + timedelta(days=1)
        with sqlite3.connect(path) as conn:
            details = conn.execute("SELECT details_enc FROM reminders").fetchone()[0]
            assert "daily" not in details and "Asia/Shanghai" not in details
            assert "Asia/Shanghai" in vault_key.decrypt(details)
        assert b"private-recurring-canary" not in path.read_bytes()
    finally:
        other_factory.close()
        factory.close()


def test_schema_19_reminder_survives_migration_without_rewriting(tmp_path, vault_key):
    import sqlite3

    from limbowave.infrastructure.database.migrations import migrate

    path = tmp_path / "legacy.db"
    due = NOW + timedelta(hours=1)
    with sqlite3.connect(path) as conn:
        migrate(conn, target=19)
        conn.execute("INSERT INTO conversations(id, title_enc, created_at) VALUES (?, ?, ?)",
                     ("c1", vault_key.encrypt("title"), NOW.isoformat()))
        conn.execute("INSERT INTO branches(id, conversation_id, created_at) VALUES (?, ?, ?)",
                     ("b1", "c1", NOW.isoformat()))
        conn.execute("INSERT INTO reminders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("reminder_legacy", "c1", "b1", due.isoformat(), due.isoformat(),
                      vault_key.encrypt("legacy private text"), NOW.isoformat(), "pending", None))
        conn.commit()
        before = conn.execute("SELECT * FROM reminders").fetchone()
        migrate(conn)
        after = conn.execute("SELECT * FROM reminders").fetchone()
        assert after[:-1] == before and after[-1] is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    factory = sqlite_uow_factory(path, vault_key)
    try:
        service = ReminderService(factory)
        item = service.list_reminders("c1")[0]
        assert item.recurrence is None and item.source_message_id is None
        message = service.deliver_due(now=due)[0]
        assert "legacy private text" in message.content
        assert service.context_for_notice("c1", "b1", message.id) == ("c1", "b1", message.id)
    finally:
        factory.close()



def test_recurring_cancel_delivery_race_stops_all_future_occurrences(tmp_path, vault_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    factory = sqlite_uow_factory(tmp_path / "recurring-race.db", vault_key)
    try:
        with factory() as uow:
            uow.conversations.add(Conversation("c1", "title", NOW))
            uow.branches.add(Branch("b1", "c1", NOW))
            uow.commit()
        creator = ReminderService(factory, clock=lambda: NOW)
        item = creator.set_reminder(
            SCOPE, reason="test", target_time="2026-10-26T23:00:00+08:00",
            recurrence="daily", timezone="Asia/Shanghai",
        )
        barrier = Barrier(2)
        def cancel():
            barrier.wait(timeout=5)
            return ReminderService(factory, clock=lambda: item.target_time).cancel_reminder(
                "c1", item.id,
            )
        def deliver():
            barrier.wait(timeout=5)
            return ReminderService(factory).deliver_due(now=item.target_time)
        with ThreadPoolExecutor(2) as pool:
            cancelled, delivered = pool.submit(cancel), pool.submit(deliver)
            assert cancelled.result().status is ReminderStatus.CANCELLED
            assert len(delivered.result()) <= 1
        assert creator.list_reminders("c1") == []
        assert creator.deliver_due(now=item.target_time + timedelta(days=20)) == []
    finally:
        factory.close()
