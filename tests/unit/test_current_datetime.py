"""当前时间工具：实时读取、参数校验、审计与远程边界。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.permissions import ExecutionMode
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_current_datetime_reads_clock_on_each_call(tmp_path: Path, mode: ExecutionMode) -> None:
    permissions = PermissionService(in_memory_uow_factory(InMemoryStore()))
    gateway = ToolGateway(permissions, tmp_path)
    first = datetime(2026, 10, 7, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
    second = first + timedelta(seconds=1)
    with patch("limbowave.application.services.tool_gateway.datetime") as clock:
        clock.now.return_value.astimezone.side_effect = [first, second]
        a = gateway.invoke("get_current_datetime", {}, "c1", mode=mode)
        b = gateway.invoke("get_current_datetime", {}, "c1", mode=mode)
    assert a.ok and b.ok
    assert a.data["datetime"] == "2026-10-07T23:59:59+08:00"
    assert a.data["date"] == "2026-10-07"
    assert a.data["time"] == "23:59:59"
    assert a.data["weekday"] == "星期三"
    assert a.data["utc_offset"] == "+0800"
    assert a.data["unix_timestamp"] == first.timestamp()
    assert b.data["date"] == "2026-10-08"
    assert b.data["weekday"] == "星期四"
    assert len(permissions.list_audit("c1")) == 2
    assert all(a.matched_rule == "clock.read" for a in permissions.list_audit("c1"))


def test_current_datetime_rejects_parameters_and_remote_origin(tmp_path: Path) -> None:
    permissions = PermissionService(in_memory_uow_factory(InMemoryStore()))
    gateway = ToolGateway(permissions, tmp_path)
    assert not gateway.invoke("get_current_datetime", {"timezone": "UTC"}, "c1").ok
    gateway.origin_provider = lambda: "web"
    assert not gateway.invoke("get_current_datetime", {}, "c1", user_confirmed=True).ok
