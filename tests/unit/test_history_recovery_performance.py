"""Recovery complexity guards: count work, never assert machine-dependent timings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from limbowave.application.branch_path import _mirror_leaf
from limbowave.application.services.compression_context import without_application_compactions
from limbowave.application.services.runtime_state_service import (
    RuntimeStateService,
    isolate_conversation_entries,
)
from limbowave.domain.conversation import Conversation
from limbowave.domain.runtime_mirror import RuntimeEntryMirror

T0 = datetime(2026, 10, 14, tzinfo=UTC)
CONVERSATION = Conversation("c", "new", T0)


class CountedEntry(dict[str, Any]):
    parent_reads: ClassVar[int] = 0

    def get(self, key: str, default: Any = None) -> Any:
        if key == "parentId":
            type(self).parent_reads += 1
        return super().get(key, default)


class CountedId(str):
    hashes: ClassVar[int] = 0

    def __hash__(self) -> int:
        type(self).hashes += 1
        return super().__hash__()


def entry(eid: str, parent: str | None = None, *, role: str = "assistant",
          old: bool = False, kind: str = "message") -> dict[str, Any]:
    return CountedEntry(
        id=eid, parentId=parent, type=kind,
        timestamp=(T0 - timedelta(days=1) if old else T0).isoformat(),
        message={"role": role, "content": eid},
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_pollution_filter_walks_each_parent_only_a_constant_number_of_times(reverse):
    rows = [entry("old", role="user", old=True), entry("safe", "old", role="user")]
    for i in range(1500):
        rows.append(entry(f"e{i}", f"e{i - 1}" if i else "safe", role="toolResult"))
    if reverse:
        rows.reverse()  # Capture order is not necessarily ancestry order.
    before = deepcopy(rows)
    CountedEntry.parent_reads = 0
    result = isolate_conversation_entries(rows, CONVERSATION)
    assert CountedEntry.parent_reads <= 6 * len(rows)
    assert [e["id"] for e in result] == [e["id"] for e in rows if e["id"] != "old"]
    assert next(e for e in result if e["id"] == "safe")["parentId"] is None
    assert rows == before


def test_shared_removed_ancestors_are_resolved_once():
    rows = [entry("old", role="user", old=True)]
    for i in range(400):
        rows.append(entry(f"s{i}", f"s{i - 1}" if i else "old", kind="compaction"))
    rows.extend(entry(f"u{i}", "s399", role="user") for i in range(400))
    CountedEntry.parent_reads = 0
    result = isolate_conversation_entries(rows, CONVERSATION)
    assert CountedEntry.parent_reads <= 6 * len(rows)
    assert len(result) == 400
    assert all(e["parentId"] is None for e in result)


def test_isolation_preserves_settings_tools_and_order_with_missing_parents():
    rows = [
        entry("reply", "summary"),
        entry("old", role="user", old=True),
        entry("config", "absent", kind="model_change"),
        entry("thinking", "config", kind="thinking_level_change"),
        entry("unknown", "absent"),
        entry("safe", "old", role="user"),
        entry("tool", "safe", role="toolResult"),
        entry("summary", "tool", kind="branch_summary"),
        entry("unrelated-cycle", "unrelated-cycle"),
    ]
    before = deepcopy(rows)
    result = isolate_conversation_entries(rows, CONVERSATION)
    assert [(e["id"], e["parentId"]) for e in result] == [
        ("reply", "tool"), ("config", None), ("thinking", "config"),
        ("safe", None), ("tool", "safe"),
    ]
    assert rows == before


def test_isolation_rejects_cycle_in_removed_parents_of_retained_config():
    rows = [entry("old", role="user", old=True), entry("safe", role="user"),
            entry("s1", "s2", kind="compaction"), entry("s2", "s1", kind="compaction"),
            entry("config", "s1", kind="model_change")]
    with pytest.raises(ValueError, match="循环"):
        isolate_conversation_entries(rows, CONVERSATION)


def test_unpolluted_fast_path_returns_original_entries():
    rows = [entry("safe", role="user"), entry("reply", "safe")]
    assert isolate_conversation_entries(rows, CONVERSATION) is rows
    assert isolate_conversation_entries(rows, None) is rows


def overlay(eid: str, parent: str | None) -> dict[str, Any]:
    return {"id": eid, "parentId": parent, "type": "compaction",
            "details": {"limbowaveVersion": "v"}}


def test_overlay_shared_parent_resolution_is_linear_and_does_not_mutate_usage():
    rows = [entry("root", role="user")]
    for i in range(400):
        rows.append(overlay(f"s{i}", CountedId(f"s{i - 1}" if i else "root")))
    rows.extend(entry(f"r{i}", CountedId("s399")) for i in range(400))
    for row in rows[401:]:
        row["message"]["usage"] = {"input": 12, "output": 3, "cost": {"total": 0.1}}
    before = deepcopy(rows)
    CountedId.hashes = 0
    result = without_application_compactions(rows)
    assert CountedId.hashes <= 40 * len(rows)
    assert [e["id"] for e in result] == ["root", *(f"r{i}" for i in range(400))]
    assert all(e["parentId"] == "root" for e in result[1:])
    assert all(e["message"]["usage"]["input"] == 0 for e in result[1:])
    assert all(e["message"]["usage"]["cost"] == {"total": 0.1} for e in result[1:])
    assert rows == before


def test_overlay_reconnects_out_of_order_chain_and_propagates_usage_reset():
    rows = [entry("root", role="user"), overlay("s2", "s1"), overlay("s1", "root"),
            entry("reply", "s2"), entry("next", "reply")]
    for row in rows[-2:]:
        row["message"]["usage"] = {"totalTokens": 9}
    result = without_application_compactions(rows)
    assert [(e["id"], e["parentId"]) for e in result] == [
        ("root", None), ("reply", "root"), ("next", "reply")]
    assert [e["message"]["usage"]["totalTokens"] for e in result[-2:]] == [0, 0]


@pytest.mark.parametrize("parent", [None, "missing"])
def test_overlay_parent_boundary_is_preserved(parent):
    result = without_application_compactions([overlay("s", parent), entry("reply", "s")])
    assert result[0]["parentId"] == parent


def test_reachable_overlay_cycle_is_rejected():
    with pytest.raises(ValueError, match="循环"):
        without_application_compactions([overlay("a", "b"), overlay("b", "a"), entry("r", "a")])
    # No retained entry references this cycle: match the existing lazy validation policy.
    assert without_application_compactions([overlay("a", "a")]) == []


def mirror(eid: str, parent: str | None = None) -> RuntimeEntryMirror:
    return RuntimeEntryMirror(id=eid, conversation_id="c", entry_id=eid, entry_type="message",
                              captured_at=T0, parent_entry_id=parent,
                              payload={"message": {"role": "assistant", "content": eid}})


def test_branch_snapshot_selects_earliest_capture_with_id_tiebreak_without_mutation():
    root = mirror("root")
    leaf = replace(mirror("leaf", "root"), id="a")
    later = replace(leaf, id="0", captured_at=T0 + timedelta(seconds=1), parent_entry_id="bad")
    tied = replace(leaf, id="z", parent_entry_id="bad")
    rows = [later, tied, leaf, root]
    snapshot = RuntimeStateService.build_branch_snapshot_from_records(None, rows, "leaf")
    assert snapshot is not None
    assert [e["id"] for e in snapshot.entries] == ["root", "leaf"]
    assert [m.id for m in rows] == ["0", "z", "a", "root"]
    assert all("id" not in m.payload for m in rows)


def test_mirror_leaf_selects_latest_tip_not_latest_parent():
    rows = [replace(mirror("root"), captured_at=T0 + timedelta(days=1)),
            mirror("tip-z", "root"), mirror("tip-a", "root")]
    assert _mirror_leaf(rows) == "tip-z"
    assert _mirror_leaf(list(reversed(rows))) == "tip-z"
    assert _mirror_leaf([]) is None
    assert _mirror_leaf([mirror("a", "b"), mirror("b", "a")]) is None


def test_uncompressed_entries_keep_original_objects_but_get_a_separate_list():
    native = {"id": "native", "type": "compaction", "parentId": "root"}
    rows = [entry("root", role="user"), native, entry("reply", "native")]
    result = without_application_compactions(rows)
    assert result == rows
    assert result is not rows
    assert all(left is right for left, right in zip(result, rows, strict=True))


def test_mirror_leaf_keeps_last_input_for_completely_equal_sort_keys():
    first = replace(mirror("first"), id="same")
    last = replace(mirror("last"), id="same")
    assert _mirror_leaf([first, last]) == "last"
    assert _mirror_leaf([last, first]) == "first"


def test_branch_snapshot_keeps_first_input_for_completely_equal_capture_keys():
    first = replace(mirror("leaf"), id="same")
    last = replace(first, parent_entry_id="root")
    snapshot = RuntimeStateService.build_branch_snapshot_from_records(
        None, [first, mirror("root"), last], "leaf"
    )
    assert snapshot is not None
    assert [e["id"] for e in snapshot.entries] == ["leaf"]


class CountedTime(datetime):
    comparisons: ClassVar[int] = 0

    def __lt__(self, other: datetime) -> bool:
        type(self).comparisons += 1
        return super().__lt__(other)

    def __gt__(self, other: datetime) -> bool:
        type(self).comparisons += 1
        return super().__gt__(other)


@pytest.mark.parametrize("operation", ["branch_snapshot", "mirror_tip"])
def test_selecting_mirrors_does_not_sort_the_whole_conversation(operation):
    import random

    start = CountedTime(2026, 10, 14, tzinfo=UTC)
    rows = [replace(mirror(f"e{i}"), captured_at=start + timedelta(microseconds=i))
            for i in range(2000)]
    random.Random(42).shuffle(rows)
    CountedTime.comparisons = 0
    if operation == "branch_snapshot":
        snapshot = RuntimeStateService.build_branch_snapshot_from_records(None, rows, "e1999")
        assert snapshot is not None
        assert snapshot.leaf_entry_id == "e1999"
    else:
        assert _mirror_leaf(rows) == "e1999"
    assert CountedTime.comparisons <= 2 * len(rows)
