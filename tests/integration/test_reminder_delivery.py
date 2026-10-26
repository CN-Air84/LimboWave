from datetime import UTC, datetime, timedelta

from limbowave.application.history_payload import history_payload
from limbowave.application.services.event_broker import EventBroker
from limbowave.application.services.reminder_scheduler import ReminderScheduler
from limbowave.application.services.reminder_service import ReminderService
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.reminder import ReminderContext
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory


async def test_delivery_without_kernel_has_own_event_scope_and_persistent_history(
    tmp_path, vault_key,
):
    factory = sqlite_uow_factory(tmp_path / "db", vault_key)
    now = datetime.now(UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("original", "old", now))
        uow.branches.add(Branch("original_branch", "original", now))
        uow.commit()
    service = ReminderService(factory, clock=lambda: now - timedelta(seconds=10))
    service.set_reminder(ReminderContext("original", "original_branch"),
                         reason="drink", delay_seconds=1)
    service._clock = lambda: now
    session = SessionController(None, uow_factory=factory)
    broker = EventBroker()
    facade = RuntimeFacade(session, factory, broker=broker)
    events = []
    unsubscribe = session.subscribe(events.append)
    before = session.coordinator().event_scope()
    try:
        await ReminderScheduler(service, session.notify_reminder).tick()
        assert session.coordinator().event_scope() == before
        assert not session.busy and not session.available
        assert [e.kind for e in events] == ["reminder"]
        wire = broker._events[-1][0]
        assert wire["conversation_id"] == "original"
        assert wire["branch_id"] == "original_branch"
        assert wire["run_id"] is None
        assert wire["message_id"] == events[0].data["message_id"]
        assert wire["payload"]["text"].startswith("⏰ 系统定时提醒")
        with factory() as uow:
            entries = history_payload(uow.messages.list_for_branch("original_branch"))
        assert len(entries) == 1 and not entries[0].can_fork
        assert entries[0].run_id is None
        await ReminderScheduler(service, session.notify_reminder).tick()
        assert len(events) == 1
    finally:
        unsubscribe()
        await facade.close()
        factory.close()
