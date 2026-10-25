"""跨会话查询：权威正文、独立逐次审批与加密存储契约。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.chat_history_service import ChatHistoryService
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.permissions import Capability, Decision, PermissionPreset, session_scope_for
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.permission_prompt import DENY, ONCE


@pytest.fixture(params=["memory", "sqlite"])
def history_stack(request, tmp_path, vault_key):
    factory = (
        sqlite_uow_factory(tmp_path / "history.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory(InMemoryStore())
    )
    now = datetime(2026, 10, 25, tzinfo=UTC)
    with factory() as uow:
        for cid, title in [("current", "当前对话"), ("other", "部署笔记"), ("third", "旅行")]:
            uow.conversations.add(Conversation(cid, title, now))
            uow.branches.add(Branch("b-" + cid, cid, now))
        uow.branches.add(Branch("fork", "other", now, parent_branch_id="b-other"))
        for mid, cid, bid, content, age in [
            ("m1", "current", "b-current", "正在聊天", 0),
            ("m2", "other", "b-other", "Docker 部署说明", 1),
            ("m3", "other", "fork", "端口配置", 1),
            ("m4", "third", "b-third", "吃饭安排", 2),
        ]:
            uow.messages.add(
                Message(
                    mid,
                    cid,
                    bid,
                    MessageRole.USER,
                    content,
                    now - timedelta(minutes=age),
                    thinking="PRIVATE_THINKING",
                )
            )
        uow.commit()
    service = ChatHistoryService(factory)
    permissions = PermissionService(factory)
    gateway = ToolGateway(permissions, tmp_path, chat_history=service)
    yield service, permissions, gateway, factory
    if hasattr(factory, "close"):
        factory.close()


def test_search_title_body_and_case_insensitive(history_stack):
    service, _, _, _ = history_stack
    assert [r["message_id"] for r in service.query(query="docker")["results"]] == ["m2"]
    data = service.query(query="部署")
    assert [r["message_id"] for r in data["results"]] == ["m3", "m2"]
    row = data["results"][1]
    assert row["conversation_id"] == "other"
    assert row["branch_id"] == "b-other"
    assert row["conversation_title"] == "部署笔记"
    assert row["role"] == "user"
    assert row["created_at"].startswith("2026-10-24T23:59")
    assert "PRIVATE_THINKING" not in json.dumps(data)
    assert "thinking" not in row and "tool_steps" not in row
    assert service.query(query="NO MATCH")["results"] == []
    # SQL wildcard characters are ordinary literal query text.
    assert service.query(query="%_' OR 1=1 --")["results"] == []


def test_pagination_and_filters_do_not_duplicate_fork_prefix(history_stack):
    service, _, _, _ = history_stack
    first = service.query(limit=2)
    assert [r["message_id"] for r in first["results"]] == ["m1", "m3"]
    assert first["count"] == 2 and first["has_more"] and first["next_offset"] == 2
    second = service.query(limit=2, offset=first["next_offset"])
    assert [r["message_id"] for r in second["results"]] == ["m2", "m4"]
    assert not second["has_more"] and second["next_offset"] is None
    assert service.query(conversation_id="other")["count"] == 2
    assert service.query(branch_id="fork")["results"][0]["message_id"] == "m3"
    assert service.query(conversation_id="third", branch_id="fork")["results"] == []
    assert service.query(conversation_id="missing")["results"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"limit": True},
        {"offset": False},
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
        {"offset": 10001},
        {"query": "x" * 501},
        {"query": 3},
        {"conversation_id": 1},
        {"branch_id": []},
        {"unknown": "x"},
    ],
)
def test_bad_params_rejected_before_approval(history_stack, params):
    _, permissions, gateway, _ = history_stack
    result = gateway.invoke("query_chat_history", params, "current", user_confirmed=True)
    assert not result.ok
    assert not result.data.get("needs_confirmation")
    assert permissions.list_audit("current") == []


def test_content_and_total_utf8_output_are_bounded(history_stack):
    service, _, _, factory = history_stack
    with factory() as uow:
        for index in range(3):
            uow.messages.add(
                Message(
                    f"long-{index}",
                    "other",
                    "b-other",
                    MessageRole.ASSISTANT,
                    "前" * 3000 + "needle" + "后" * 3000,
                    datetime(2026, 10, 26, tzinfo=UTC),
                )
            )
        uow.commit()
    data = service.query(query="needle", max_output_bytes=1400)
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= 1400
    assert data["count"] >= 1
    assert data["has_more"] and data["next_offset"] == data["count"]
    assert data["results"][0]["content_truncated"]
    assert "needle" in data["results"][0]["content"]
    assert data["output_truncated"]
    seen = [r["message_id"] for r in data["results"]]
    while data["has_more"]:
        data = service.query(query="needle", offset=data["next_offset"], max_output_bytes=1400)
        seen.extend(r["message_id"] for r in data["results"])
    assert seen == ["long-2", "long-1", "long-0"]
    # No plaintext search index or edits to saved content.
    with factory() as uow:
        assert len(uow.messages.get("long-0").content) == 6006


@pytest.mark.parametrize("preset", list(PermissionPreset))
def test_gateway_requires_independent_history_permission(history_stack, preset):
    _, permissions, gateway, _ = history_stack
    permissions.set_preset("current", preset)
    permissions.grant("current", Capability.FILE_READ, allowed_paths=(str(gateway.workspace_root),))
    with pytest.raises(ValueError, match=r"不支持.*持久授权"):
        permissions.grant("current", Capability.HISTORY_READ)
    for _ in range(2):
        result = gateway.invoke("query_chat_history", {"query": "Docker"}, "current")
        assert result.ok is (preset is PermissionPreset.FULL_ACCESS)
        if preset is not PermissionPreset.FULL_ACCESS:
            assert result.data["needs_confirmation"]
    approved = gateway.invoke("query_chat_history", {}, "current", user_confirmed=True)
    assert approved.ok and approved.data["count"] == 4
    request = gateway.prepare_request("query_chat_history", {})
    assert session_scope_for(request, str(gateway.workspace_root)) is None
    assert any(
        a.user_confirmed and a.decision is Decision.ALLOW for a in permissions.list_audit("current")
    )


@pytest.mark.parametrize("preset", list(PermissionPreset))
@pytest.mark.parametrize("answer", [ONCE, DENY])
async def test_model_gate_asks_every_time_except_full_access(
    history_stack, monkeypatch, preset, answer
):
    _, permissions, gateway, _ = history_stack
    permissions.set_preset("current", preset)
    asked = []

    async def ask(window, request, reason, scope):
        asked.append((request, reason, scope))
        return answer

    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(None, permissions, lambda: "current", gateway)
    for _ in range(2):
        allowed = await handler(
            "limbowave.gate:query_chat_history",
            json.dumps(
                {
                    "tool": "query_chat_history",
                    "input": {"query": "Docker", "conversation_id": "other"},
                }
            ),
        )
        assert allowed is (preset is PermissionPreset.FULL_ACCESS or answer == ONCE)
    assert len(asked) == (0 if preset is PermissionPreset.FULL_ACCESS else 2)
    assert all(scope is None for _, _, scope in asked)
    if answer == DENY and preset is not PermissionPreset.FULL_ACCESS:
        assert sum(a.decision is Decision.DENY for a in permissions.list_audit("current")) == 2


async def test_stale_conversation_or_remote_origin_cannot_use_approval(history_stack, monkeypatch):
    _, permissions, gateway, _ = history_stack
    holder = ["current"]

    async def ask(*_):
        holder[0] = "other"
        return ONCE

    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(None, permissions, lambda: holder[0], gateway)
    detail = json.dumps({"tool": "query_chat_history", "input": {}})
    assert not await handler("limbowave.gate:query_chat_history", detail)
    holder[0] = "current"
    gateway.origin_provider = lambda: "web"
    permissions.set_preset("current", PermissionPreset.FULL_ACCESS)
    assert not await handler("limbowave.gate:query_chat_history", detail)
    assert not gateway.invoke("query_chat_history", {}, "current", user_confirmed=True).ok


def test_unconfigured_service_is_honest(history_stack, tmp_path):
    _, permissions, _, _ = history_stack
    gateway = ToolGateway(permissions, tmp_path)
    result = gateway.invoke("query_chat_history", {}, "current", user_confirmed=True)
    assert not result.ok and "未配置" in result.error


def test_fabricated_history_grant_cannot_bypass_per_call_rule():
    from limbowave.domain.permissions import PermissionGrant, ToolRequest, evaluate

    grant = PermissionGrant("g", "current", Capability.HISTORY_READ, datetime.now(UTC))
    request = ToolRequest("query_chat_history", Capability.HISTORY_READ)
    assert evaluate(request, [grant]).decision is Decision.CONFIRM


def test_no_history_content_is_decrypted_before_permission(history_stack, monkeypatch):
    from limbowave.infrastructure.crypto.vault import VaultKey

    _, _, gateway, _ = history_stack
    decrypted = []
    original = VaultKey.decrypt

    def track(key, blob):
        text = original(key, blob)
        decrypted.append(text)
        return text

    monkeypatch.setattr(VaultKey, "decrypt", track)
    result = gateway.invoke("query_chat_history", {"query": "Docker"}, "current")
    assert not result.ok and result.data["needs_confirmation"]
    assert "部署笔记" not in decrypted
    assert "Docker 部署说明" not in decrypted
    assert "PRIVATE_THINKING" not in decrypted


def test_stream_projection_omits_private_fields(history_stack):
    _, _, _, factory = history_stack
    with factory() as uow:
        messages = uow.messages.iter_chat_history(conversation_id="other")
        try:
            rows = list(messages)
        finally:
            messages.close()
    assert len(rows) == 2
    assert all(not row.thinking and not row.tool_steps and not row.segments for row in rows)


def test_unicode_casefold_excerpt_maps_back_to_original_content(history_stack):
    service, _, _, factory = history_stack
    with factory() as uow:
        uow.messages.add(
            Message(
                "unicode",
                "other",
                "b-other",
                MessageRole.USER,
                "ß" * 3000 + "NEEDLE" + "尾" * 3000,
                datetime(2026, 10, 26, tzinfo=UTC),
            )
        )
        uow.commit()
    data = service.query(query="needle", max_output_bytes=1500)
    row = data["results"][0]
    assert "NEEDLE" in row["content"]
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= 1500


def test_all_app_tool_schemas_and_capabilities_agree():
    from limbowave.application.services.tool_gateway import _SCHEMAS, capability_of
    from limbowave.infrastructure.pi_runtime.environment_builder import APP_TOOLS

    assert set(APP_TOOLS) == set(_SCHEMAS)
    assert all(capability_of(tool) for tool in APP_TOOLS)


async def test_invalid_model_args_do_not_open_dialog(history_stack, monkeypatch):
    from unittest.mock import AsyncMock

    _, permissions, gateway, _ = history_stack
    ask = AsyncMock(side_effect=AssertionError("不应弹窗"))
    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(None, permissions, lambda: "current", gateway)
    assert not await handler(
        "limbowave.gate:query_chat_history",
        json.dumps(
            {
                "tool": "query_chat_history",
                "input": {"limit": True},
            }
        ),
    )
    ask.assert_not_called()
    assert permissions.list_audit("current") == []


async def test_origin_change_during_dialog_invalidates_approval(history_stack, monkeypatch):
    _, permissions, gateway, _ = history_stack
    origin = ["desktop"]
    gateway.origin_provider = lambda: origin[0]

    async def ask(*_):
        origin[0] = "web"
        return ONCE

    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    handler = _make_permission_handler(None, permissions, lambda: "current", gateway)
    assert not await handler(
        "limbowave.gate:query_chat_history",
        json.dumps(
            {
                "tool": "query_chat_history",
                "input": {},
            }
        ),
    )


def test_sqlite_query_never_decrypts_private_payload_columns(history_stack):
    from contextlib import closing

    from limbowave.infrastructure.database.sqlite_repositories import open_connection

    service, _, _, factory = history_stack
    if not hasattr(factory, "_path"):
        pytest.skip("SQLite 密文投影专用测试")
    with closing(open_connection(factory._path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET thinking_enc = ?, tool_steps_enc = ?, segments_enc = ? "
            "WHERE id = ?",
            ("INVALID", "INVALID", "INVALID", "m2"),
        )
    # 如果查询复用完整消息 _to_domain，这些损坏的私有密文会导致解密失败。
    assert service.query(query="Docker")["results"][0]["content"] == "Docker 部署说明"
