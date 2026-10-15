"""Synthetic recovery microbenchmarks; no user data, network, database or GUI.

Run: .venv/Scripts/python scripts/benchmark_history_recovery.py --output .var/recovery.json
Fixtures are built outside timed sections. Results are medians, not end-to-end UI latency.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from limbowave.application.branch_path import _mirror_leaf
from limbowave.application.services.compression_context import without_application_compactions
from limbowave.application.services.runtime_state_service import (
    RuntimeStateService,
    isolate_conversation_entries,
)
from limbowave.domain.conversation import Conversation
from limbowave.domain.runtime_mirror import RuntimeEntryMirror

T0 = datetime(2026, 10, 14, tzinfo=UTC)


def workloads(size: int) -> dict[str, Callable[[], object]]:
    def message(eid: str, parent: str | None, role: str = "assistant") -> dict[str, Any]:
        return {"id": eid, "parentId": parent, "type": "message",
                "timestamp": T0.isoformat(), "message": {"role": role, "content": eid}}

    old = message("old", None, "user")
    old["timestamp"] = (T0 - timedelta(days=1)).isoformat()
    conversation = Conversation("c", "new", T0)
    polluted = [old, message("safe", "old", "user")]
    polluted.extend(message(f"e{i}", f"e{i - 1}" if i else "safe") for i in range(size))
    overlays: list[dict[str, Any]] = [message("root", None, "user")]
    overlays.extend(
        {"id": f"s{i}", "parentId": f"s{i - 1}" if i else "root", "type": "compaction",
         "details": {"limbowaveVersion": "v"}} for i in range(size)
    )
    overlays.extend(message(f"r{i}", f"s{size - 1}") for i in range(size))
    mirrors = [RuntimeEntryMirror(
        id=f"m{i}", conversation_id="c", entry_id=f"e{i}", entry_type="message",
        captured_at=T0 + timedelta(microseconds=i), parent_entry_id="root",
    ) for i in range(size)]
    random.Random(42).shuffle(mirrors)
    clean = polluted[1:]
    return {
        "clean_history_ms": lambda: isolate_conversation_entries(clean, conversation),
        "no_overlay_ms": lambda: without_application_compactions(clean),
        "branch_snapshot_ms": lambda: RuntimeStateService.build_branch_snapshot_from_records(
            None, mirrors, f"e{size - 1}"
        ),
        "polluted_ancestry_ms": lambda: isolate_conversation_entries(polluted, conversation),
        "shared_overlay_ancestry_ms": lambda: without_application_compactions(overlays),
        "mirror_tip_ms": lambda: _mirror_leaf(mirrors),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[500, 1500, 4000])
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and samples must be positive")
    rows = []
    for size in args.sizes:
        row: dict[str, int | float] = {"size": size}
        for name, work in workloads(size).items():
            work()  # Warm up imports and allocations before measurement.
            timings = []
            for _ in range(args.samples):
                start = time.perf_counter()
                work()
                timings.append((time.perf_counter() - start) * 1000)
            row[name] = round(statistics.median(timings), 3)
        rows.append(row)
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "samples": args.samples, "results": rows}
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
