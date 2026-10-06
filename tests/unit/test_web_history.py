"""Both backends must page before decrypting, preserving logical ancestor order."""

from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.branch_path import branch_messages
from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.application.services.history_service import HistoryService
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

T0 = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    factory = (
        in_memory_uow_factory()
        if request.param == "memory"
        else sqlite_uow_factory(tmp_path / "pages.db", vault_key)
    )
    with factory() as uow:
        for cid in ("c", "other"):
            uow.conversations.add(Conversation(cid, "Title", T0))
            uow.branches.add(Branch(cid, cid, T0))
        for i in range(12):
            uow.messages.add(
                Message(
                    id=f"m{i:02}",
                    conversation_id="c",
                    branch_id="c",
                    role=MessageRole.USER,
                    content=f"body {i}",
                    thinking="thought",
                    created_at=T0 + timedelta(seconds=i // 2),
                )
            )
        uow.commit()
    return factory


def collect(service, branch="c", size=3):
    cursor = None
    result = []
    cursors = set()
    while True:
        page = service.page_messages("c", branch, size, cursor)
        assert set(page) == {"items", "next_cursor"}
        assert len(page["items"]) <= size
        result = [m["id"] for m in page["items"]] + result
        cursor = page["next_cursor"]
        if cursor is None:
            return result
        assert cursor not in cursors
        cursors.add(cursor)


@pytest.mark.parametrize("size", [1, 3, 5, 12, 100])
def test_keyset_and_projection(factory, size):
    service = HistoryService(factory)
    assert collect(service, size=size) == [f"m{i:02}" for i in range(12)]
    item = service.page_messages("c", "c")["items"][0]
    assert set(item) == {
        "id",
        "role",
        "content",
        "thinking",
        "created_at",
        "status",
        "run_id",
        "tools",
    }
    assert item["thinking"] == "thought"


@pytest.mark.parametrize("include", [True, False])
@pytest.mark.parametrize("fork", ["m04", "missing"])
def test_nested_fork_into_grandparent_and_missing_fallback(factory, include, fork):
    with factory() as uow:
        uow.branches.add(
            Branch(
                "child",
                "c",
                T0 + timedelta(seconds=3),
                parent_branch_id="c",
                forked_from_message_id="m07",
            )
        )
        uow.messages.add(
            Message(
                "child-m", "c", "child", MessageRole.ASSISTANT, "child", T0 + timedelta(seconds=4)
            )
        )
        uow.branches.add(
            Branch(
                "leaf",
                "c",
                T0 + timedelta(seconds=2),
                parent_branch_id="child",
                forked_from_message_id=fork,
                include_fork_message=include,
            )
        )
        # Earlier timestamp than ancestors: path order still wins over global time order.
        uow.messages.add(Message("leaf-m", "c", "leaf", MessageRole.USER, "leaf", T0))
        uow.commit()
    with factory() as uow:
        expected = [m.id for m in branch_messages(uow, "leaf")]
    assert collect(HistoryService(factory), "leaf", 2) == expected


def test_inserts_do_not_shift_cursor(factory):
    service = HistoryService(factory)
    page = service.page_messages("c", "c", 3)
    with factory() as uow:
        uow.messages.add(Message("new", "c", "c", MessageRole.USER, "new", T0 + timedelta(days=1)))
        uow.commit()
    older = service.page_messages("c", "c", 100, page["next_cursor"])
    assert [m["id"] for m in older["items"]] == [f"m{i:02}" for i in range(9)]


def test_conversation_pages_and_active_branch(factory):
    with factory() as uow:
        uow.branches.add(Branch("new-branch", "c", T0 + timedelta(seconds=1)))
        uow.conversations.add(Conversation("orphan", "No branch", T0))
        uow.commit()
    service = HistoryService(factory)
    first = service.page_conversations(1)
    assert first["items"][0]["id"] == "other"
    second = service.page_conversations(1, first["next_cursor"])
    assert second["items"][0]["branch_id"] == "c"  # older branch has later activity
    assert set(second["items"][0]) == {"id", "title", "branch_id", "created_at"}
    assert second["next_cursor"] is None


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.2, "50"])
def test_invalid_limits(factory, limit):
    service = HistoryService(factory)
    with pytest.raises(RuntimeConflict):
        service.page_messages("c", "c", limit)
    with pytest.raises(RuntimeConflict):
        service.page_conversations(limit)


@pytest.mark.parametrize("cursor", ["", "not a cursor", "e30", "x" * 4097])
def test_invalid_cursors(factory, cursor):
    service = HistoryService(factory)
    with pytest.raises(RuntimeConflict):
        service.page_messages("c", "c", cursor=cursor)
    with pytest.raises(RuntimeConflict):
        service.page_conversations(cursor=cursor)


def test_cursor_scope_and_branch_ownership(factory):
    service = HistoryService(factory)
    cursor = service.page_messages("c", "c", 1)["next_cursor"]
    for cid, bid in [("other", "other"), ("other", "c"), ("c", "missing")]:
        with pytest.raises(RuntimeConflict):
            service.page_messages(cid, bid, cursor=cursor)
    with pytest.raises(RuntimeConflict):
        service.page_messages("other", "c")
    with pytest.raises(RuntimeConflict):
        service.page_conversations(cursor=cursor)
    assert service.page_messages("other", "other") == {"items": [], "next_cursor": None}


def test_no_unbounded_reader_calls(factory, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded/decrypting history reader used")

    with factory() as uow:
        monkeypatch.setattr(type(uow.messages), "list_for_branch", forbidden)
        monkeypatch.setattr(type(uow.conversations), "list_all", forbidden)
        monkeypatch.setattr(type(uow.branches), "list_for_conversation", forbidden)
    assert len(HistoryService(factory).page_messages("c", "c", 2)["items"]) == 2
    assert len(HistoryService(factory).page_conversations(1)["items"]) == 1


def test_sqlite_decryption_is_bounded(tmp_path, vault_key, monkeypatch):
    factory = sqlite_uow_factory(tmp_path / "large.db", vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "title", T0))
        uow.branches.add(Branch("b", "c", T0))
        for i in range(1000):
            uow.messages.add(
                Message(f"m{i:04}", "c", "b", MessageRole.USER, "body", T0, thinking="thought")
            )
        uow.commit()
    calls = []
    original = type(vault_key).decrypt

    def decrypt(key, ciphertext):
        calls.append(ciphertext)
        return original(key, ciphertext)

    monkeypatch.setattr(type(vault_key), "decrypt", decrypt)
    service = HistoryService(factory)
    assert len(service.page_messages("c", "b", 5)["items"]) == 5
    assert len(calls) == 18  # five rows + lookahead: content, thinking, tool metadata
    calls.clear()
    assert len(service.page_conversations(5)["items"]) == 1
    assert len(calls) == 1  # title only, no messages


def test_cycle_and_cross_conversation_ancestor(factory):
    with factory() as uow:
        uow.branches.add(Branch("bad", "c", T0, parent_branch_id="other"))
        uow.branches.add(Branch("cycle", "c", T0, parent_branch_id="cycle"))
        uow.commit()
    for branch in ("bad", "cycle"):
        with pytest.raises(RuntimeConflict):
            HistoryService(factory).page_messages("c", branch)


def test_partial_status_and_tool_metadata_survive_reload_without_tool_secrets(factory):
    from limbowave.domain.conversation import MessageStatus
    from limbowave.domain.tool_step import ToolStatus, ToolStep
    from limbowave.web.dto import page

    with factory() as uow:
        uow.messages.add(
            Message(
                "partial",
                "c",
                "c",
                MessageRole.ASSISTANT,
                "partial answer",
                T0 + timedelta(hours=1),
                status=MessageStatus.PARTIAL,
                tool_steps=(
                    ToolStep(
                        "tool-call",
                        "read_file",
                        ToolStatus.ERROR,
                        args={"path": "SECRET"},
                        error="SECRET",
                    ),
                ),
            )
        )
        uow.commit()
    result = page(HistoryService(factory).page_messages("c", "c", 1), "message")
    assert result["items"][0]["status"] == "partial"
    assert result["items"][0]["tools"] == [
        {"tool_id": "tool-call", "name": "read_file", "status": "failed"}
    ]
    assert "SECRET" not in str(result)
