"""The tools share the real gateway and cannot select another conversation."""
from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.reminder_service import DELIVERY_NOTICE, ReminderService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.reminder import ReminderContext
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory


@pytest.fixture
def gateway(tmp_path):
    factory = in_memory_uow_factory()
    now = datetime.now(UTC)
    with factory() as uow:
        for c, b in [("c1", "b1"), ("c2", "b2")]:
            uow.conversations.add(Conversation(c, c, now))
            uow.branches.add(Branch(b, c, now))
        uow.commit()
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.reminder_service = ReminderService(factory)
    gateway.reminder_context = lambda: ReminderContext("c1", "b1")
    return gateway


def test_confirmation_create_list_cancel(gateway):
    params = {"reason": "喝水", "delay_seconds": 3600}
    denied = gateway.invoke("set_reminder", params, "c1")
    assert denied.data["needs_confirmation"]
    created = gateway.invoke("set_reminder", params, "c1", user_confirmed=True)
    assert created.ok and created.data["scheduled"]
    assert created.data["delivery_notice"] == DELIVERY_NOTICE
    assert created.data["branch_id"] == "b1"
    listed = gateway.invoke("list_reminders", {}, "c1")
    assert listed.ok and listed.data["reminders"][0]["id"] == created.data["id"]
    cancelled = gateway.invoke("cancel_reminder", {"reminder_id": created.data["id"]}, "c1")
    assert cancelled.ok and cancelled.data["cancelled"]
    assert gateway.invoke("list_reminders", {}, "c1").data["reminders"] == []


@pytest.mark.parametrize("tool, params", [
    ("set_reminder", {"reason": "test", "delay_seconds": 60}),
    ("list_reminders", {}), ("cancel_reminder", {"reminder_id": "unknown"}),
])
def test_remote_origin_is_denied_even_with_confirmation(gateway, tool, params):
    gateway.origin_provider = lambda: "web"
    result = gateway.invoke(tool, params, "c1", user_confirmed=True)
    assert not result.ok and "web" in result.error


def test_unavailable_or_stale_context(gateway):
    assert not gateway.invoke("list_reminders", {}, "c2", user_confirmed=True).ok
    gateway.reminder_context = lambda: None
    assert not gateway.invoke("list_reminders", {}, "c1", user_confirmed=True).ok
    gateway.reminder_context = lambda: ReminderContext("c1", "b1")
    gateway.reminder_service = None
    assert not gateway.invoke("list_reminders", {}, "c1", user_confirmed=True).ok


@pytest.mark.parametrize("tool, params", [
    ("set_reminder", {"reason": "test", "delay_seconds": True}),
    ("set_reminder", {"reason": "test", "delay_seconds": 2, "conversation_id": "c2"}),
    ("set_reminder", {"reason": "test", "target_time": "2026-10-26T20:00:00"}),
    ("list_reminders", {"limit": True}), ("list_reminders", {"status": "nope"}),
    ("list_reminders", {"offset": -1}), ("cancel_reminder", {"reminder_id": ""}),
])
def test_invalid_params_fail_before_authorization(gateway, tool, params):
    assert not gateway.invoke(tool, params, "c1", user_confirmed=True).ok
    assert gateway._permissions.list_audit("c1") == []


def test_output_limit_keeps_whole_rows_and_resumable_cursor(gateway):
    for _ in range(5):
        assert gateway.invoke("set_reminder", {
            "reason": "水" * 1000, "delay_seconds": 60,
        }, "c1", user_confirmed=True).ok
    gateway._max_output_bytes = 7500
    first = gateway.invoke("list_reminders", {}, "c1")
    assert first.ok and first.truncated and first.data["has_more"]
    offset = first.data["next_offset"]
    assert offset == len(first.data["reminders"])
    following = gateway.invoke("list_reminders", {"offset": offset}, "c1")
    assert following.ok
    assert {r["id"] for r in first.data["reminders"]}.isdisjoint(
        r["id"] for r in following.data["reminders"]
    )



def test_recurring_tool_requires_confirmation_and_preserves_rule(gateway):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0,
    )
    params = {"reason": "准备休息", "target_time": tomorrow.isoformat(),
              "recurrence": "daily", "timezone": "Asia/Shanghai"}
    pending = gateway.invoke("set_reminder", params, "c1")
    assert pending.data["needs_confirmation"] and "周期" in pending.error
    result = gateway.invoke("set_reminder", params, "c1", user_confirmed=True)
    assert result.ok and result.data["scheduled"]
    assert result.data["recurrence"]["frequency"] == "daily"
    assert result.data["recurrence"]["local_time"] == "23:00:00"
    assert gateway.invoke("list_reminders", {}, "c1").data["reminders"][0]["recurrence"] == (
        result.data["recurrence"]
    )


@pytest.mark.parametrize("extra", [
    {"recurrence": "daily"}, {"recurrence": "hourly", "timezone": "Asia/Shanghai"},
    {"recurrence": "daily", "timezone": "Unknown/Zone"},
    {"recurrence": "daily", "timezone": "Asia/Shanghai"},
    {"source_message_id": "foreign"},
])
def test_invalid_recurring_params_fail_before_authorization(gateway, extra):
    result = gateway.invoke("set_reminder", {"reason": "test", "delay_seconds": 60, **extra}, "c1")
    assert not result.ok and not result.data.get("needs_confirmation")
    assert gateway._permissions.list_audit("c1") == []



def test_origin_turn_change_in_same_branch_is_rejected_before_execution(gateway, monkeypatch):
    gateway.reminder_context = lambda: ReminderContext("c1", "b1", "old-turn")
    original = gateway._permissions.authorize
    def authorize(*args, **kwargs):
        result = original(*args, **kwargs)
        gateway.reminder_context = lambda: ReminderContext("c1", "b1", "new-turn")
        return result
    monkeypatch.setattr(gateway._permissions, "authorize", authorize)
    result = gateway.invoke("set_reminder", {"reason": "test", "delay_seconds": 60},
                            "c1", user_confirmed=True)
    assert not result.ok and "活动会话已改变" in result.error
    assert gateway.reminder_service.list_reminders("c1") == []
