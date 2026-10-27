"""HIST/PRIV: ordinary search must authorize the owning user turn before paging."""

import json
from datetime import timedelta

import pytest

from limbowave.application.services.privacy_service import PrivacyService
from limbowave.domain.conversation import Branch, Message, MessageRole
from tests.unit.test_chat_history import history_stack as history_stack


def seed(factory, rows):
    rule = PrivacyService(factory).add("restricted topic")
    now = rule.created_at + timedelta(seconds=1)
    with factory() as uow:
        for index, (mid, branch, role, text) in enumerate(rows):
            uow.messages.add(Message(
                mid, "third", branch, role, text, now + timedelta(seconds=index),
                run_id="public-run", thinking="PRIVATE_THINKING",
            ))
        uow.commit()
    return now


def ids(data):
    return [row["message_id"] for row in data["results"]]


@pytest.mark.parametrize("query", ["", "answer"])
def test_search_checks_excluded_turn_before_pagination(history_stack, query):
    service, _, _, factory = history_stack
    seed(factory, [
        ("safe-u", "b-third", MessageRole.USER, "ordinary request"),
        ("safe-a", "b-third", MessageRole.ASSISTANT, "safe answer"),
        ("private-u", "b-third", MessageRole.USER, "restricted topic"),
        ("private-a", "b-third", MessageRole.ASSISTANT, "opaque answer"),
        ("private-a2", "b-third", MessageRole.ASSISTANT, "another answer"),
    ])
    first = service.query(query=query, conversation_id="third", limit=1)
    assert ids(first) == ["safe-a"]
    if query:
        assert not first["has_more"] and first["next_offset"] is None
    else:
        second = service.query(conversation_id="third", limit=1, offset=first["next_offset"])
        assert ids(second) == ["safe-u"]
        third = service.query(conversation_id="third", limit=1, offset=second["next_offset"])
        assert ids(third) == ["m4"] and not third["has_more"]
    assert "opaque answer" not in json.dumps(service.query(conversation_id="third"))


def test_search_resumes_after_a_safe_new_user_turn_and_does_not_block_siblings(history_stack):
    service, _, _, factory = history_stack
    now = seed(factory, [
        ("safe-u", "b-third", MessageRole.USER, "ordinary request"),
        ("private-u", "b-third", MessageRole.USER, "restricted topic"),
        ("private-a", "b-third", MessageRole.ASSISTANT, "hidden answer"),
        ("next-u", "b-third", MessageRole.USER, "ordinary followup"),
        ("next-a", "b-third", MessageRole.ASSISTANT, "visible answer"),
    ])
    with factory() as uow:
        for bid, fork in [("safe-child", "safe-u"), ("private-child", "private-u")]:
            uow.branches.add(Branch(bid, "third", now + timedelta(seconds=20),
                                    parent_branch_id="b-third", forked_from_message_id=fork,
                                    include_fork_message=True))
            uow.messages.add(Message(bid + "-a", "third", bid, MessageRole.ASSISTANT,
                                     "branch answer", now + timedelta(seconds=21), run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="safe-child")) == ["safe-child-a"]
    assert ids(service.query(query="answer", branch_id="private-child")) == []
    result = service.query(query="answer", conversation_id="third")
    assert ids(result) == ["safe-child-a", "next-a"]


def test_scan_cap_does_not_poison_older_verified_turns(history_stack, monkeypatch):
    from limbowave.application.services.chat_history_service import MAX_PRIVACY_TURN_MESSAGES

    service, _, _, factory = history_stack
    now = seed(factory, [("long-u", "b-third", MessageRole.USER, "ordinary request")])
    with factory() as uow:
        repo_type = type(uow.messages)
        for n in range(MAX_PRIVACY_TURN_MESSAGES + 2):
            uow.messages.add(Message(
                f"long-{n:03}", "third", "b-third", MessageRole.ASSISTANT,
                "answer", now + timedelta(seconds=n + 1), run_id="r",
            ))
        uow.commit()
    reads = []
    original = repo_type.page_chat_history

    def record(self, *args, **kwargs):
        rows = original(self, *args, **kwargs)
        reads.append(len(rows))
        return rows

    monkeypatch.setattr(repo_type, "page_chat_history", record)
    data = service.query(query="answer", conversation_id="third", limit=1)
    assert ids(data) == [f"long-{MAX_PRIVACY_TURN_MESSAGES - 2:03}"]
    assert max(reads) <= 8
    assert sum(reads) <= 5 * MAX_PRIVACY_TURN_MESSAGES


@pytest.mark.parametrize("include", [False, True])
def test_fork_user_replacement_and_inherited_user_obey_inclusion(history_stack, include):
    service, _, _, factory = history_stack
    now = seed(factory, [
        ("safe-u", "b-third", MessageRole.USER, "ordinary request"),
        ("fork-u", "b-third", MessageRole.USER, "restricted topic"),
    ])
    with factory() as uow:
        uow.branches.add(Branch("forked", "third", now + timedelta(seconds=3),
                                parent_branch_id="b-third", forked_from_message_id="fork-u",
                                include_fork_message=include))
        uow.messages.add(Message("fork-a", "third", "forked", MessageRole.ASSISTANT,
                                 "branch answer", now + timedelta(seconds=4), run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="forked")) == ([] if include else ["fork-a"])


def test_equal_time_user_id_order_and_source_id_exclusions(history_stack):
    service, _, _, factory = history_stack
    privacy = PrivacyService(factory)
    rule = privacy.add("label not in the text", source_ids=["eq-1"])
    now = rule.created_at + timedelta(seconds=1)
    with factory() as uow:
        for mid, role in [("eq-1", MessageRole.USER), ("eq-2", MessageRole.ASSISTANT),
                          ("eq-3", MessageRole.USER), ("eq-4", MessageRole.ASSISTANT)]:
            uow.messages.add(Message(mid, "third", "b-third", role, "answer", now, run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", conversation_id="third")) == ["eq-4", "eq-3"]


def test_search_unknown_owning_user_fails_closed(history_stack):
    service, _, _, factory = history_stack
    now = seed(factory, [])
    with factory() as uow:
        uow.branches.add(Branch("orphan", "third", now))
        uow.messages.add(Message("orphan-a", "third", "orphan", MessageRole.ASSISTANT,
                                 "answer", now + timedelta(seconds=1), run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="orphan")) == []


def test_search_uses_only_public_ciphertext_for_turn_provenance(history_stack):
    from contextlib import closing

    from limbowave.infrastructure.database.sqlite_repositories import open_connection

    service, _, _, factory = history_stack
    seed(factory, [
        ("safe-u", "b-third", MessageRole.USER, "ordinary request"),
        ("safe-a", "b-third", MessageRole.ASSISTANT, "safe answer"),
    ])
    if not hasattr(factory, "_path"):
        pytest.skip("SQLite-only ciphertext projection")
    with closing(open_connection(factory._path)) as conn, conn:
        conn.execute("UPDATE messages SET thinking_enc=?, tool_steps_enc=?, segments_enc=?",
                     ("INVALID", "INVALID", "INVALID"))
    assert ids(service.query(query="answer", branch_id="b-third")) == ["safe-a"]


def test_nested_fork_uses_earlier_inherited_user_not_parent_later_user(history_stack):
    service, _, _, factory = history_stack
    now = seed(factory, [
        ("safe-u", "b-third", MessageRole.USER, "ordinary request"),
        ("later-private-u", "b-third", MessageRole.USER, "restricted topic"),
    ])
    with factory() as uow:
        uow.branches.add(Branch("parent-child", "third", now + timedelta(seconds=2),
                                parent_branch_id="b-third",
                                forked_from_message_id="later-private-u",
                                include_fork_message=True))
        uow.branches.add(Branch("nested", "third", now + timedelta(seconds=3),
                                parent_branch_id="parent-child", forked_from_message_id="safe-u",
                                include_fork_message=True))
        uow.messages.add(Message("nested-a", "third", "nested", MessageRole.ASSISTANT,
                                 "branch answer", now + timedelta(seconds=4), run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="nested")) == ["nested-a"]


def test_search_preserves_missing_run_and_old_derived_quarantine(history_stack):
    service, _, _, factory = history_stack
    now = seed(factory, [("safe-u", "b-third", MessageRole.USER, "ordinary request")])
    with factory() as uow:
        uow.messages.add(Message("old-u", "third", "b-third", MessageRole.USER,
                                 "ordinary old request", now - timedelta(seconds=3)))
        uow.messages.add(Message("old-a", "third", "b-third", MessageRole.ASSISTANT,
                                 "answer", now - timedelta(seconds=2), run_id="r"))
        uow.messages.add(Message("untraced-a", "third", "b-third", MessageRole.ASSISTANT,
                                 "answer", now + timedelta(seconds=1)))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="b-third")) == []


def test_search_corrupt_ancestry_fails_closed(history_stack):
    service, _, _, factory = history_stack
    now = seed(factory, [])
    with factory() as uow:
        uow.branches.add(Branch("cycle", "third", now, parent_branch_id="cycle"))
        uow.messages.add(Message("cycle-a", "third", "cycle", MessageRole.ASSISTANT,
                                 "answer", now + timedelta(seconds=1), run_id="r"))
        uow.commit()
    assert ids(service.query(query="answer", branch_id="cycle")) == []
