"""Synthetic chat probes, not compositor FPS or provider latency; no real profile.

Snapshots passed with --chat-source/--rpc-source allow same-machine comparisons.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from limbowave.infrastructure import pi_rpc
from limbowave.ui import chat_view


def _module(path: Path | None, default: Any, name: str) -> Any:
    if path is None:
        return default
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dispose(view: QWidget) -> None:
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _stream(view_type: Any, delta: str, count: int) -> dict[str, Any]:
    view = view_type()
    view.resize(900, 650)
    view.show()
    view.set_busy(True)
    started = time.perf_counter()
    view.append_assistant_delta("first\n")
    first_ms = (time.perf_counter() - started) * 1000
    body = view._stream_row.label
    first_immediate = body.toPlainText() == "first\n"
    QTest.qWait(80)
    changes: list[int] = []
    body.document().contentsChanged.connect(lambda: changes.append(1))
    started = time.perf_counter()
    for _ in range(count):
        view.append_assistant_delta(delta)
    enqueue_ms = (time.perf_counter() - started) * 1000
    expected = "first\n" + delta * count
    deadline = time.perf_counter() + 10
    while body.toPlainText() != expected and time.perf_counter() < deadline:
        QTest.qWait(1)
    result = {
        "deltas": count, "chars": len(expected), "document_updates": len(changes),
        "first_delta_ms": first_ms, "first_delta_immediate": first_immediate,
        "enqueue_ms": enqueue_ms,
        "settled_ms": (time.perf_counter() - started) * 1000,
        "exact": body.toPlainText() == expected,
    }
    _dispose(view)
    if not result["exact"] or not first_immediate:
        raise RuntimeError("Streaming sample lost text or delayed its first fragment")
    return result


def _history(view_type: Any) -> dict[str, Any]:
    view = view_type()
    view.resize(900, 650)
    view.show()
    history = [
        ("user" if i % 2 == 0 else "assistant",
         f"Message {i}\n\n" + ("Question" if i % 2 == 0 else
                               "**Answer**\n\n```python\nprint('hello')\n```"),
         "", f"m{i}") for i in range(300)
    ]
    view.load_history(history)
    QTest.qWait(80)
    pages = []
    for _ in range(3):
        before = {id(row) for row in view._rows}
        started = time.perf_counter()
        view._load_earlier()
        elapsed = (time.perf_counter() - started) * 1000
        preserved = sum(id(row) in before for row in view._rows)
        QTest.qWait(50)
        pages.append({"call_ms": elapsed, "rows": len(view._rows),
                      "existing_rows": len(before), "preserved_rows": preserved})
    _dispose(view)
    return {"pages": pages}


async def _transport(module: Any) -> dict[str, Any]:
    reader = asyncio.StreamReader()
    total = 10_000
    reader.feed_data(b'{"type":"message_update","delta":"x"}\n' * total)
    reader.feed_eof()
    rpc = module.PiRpcProcess(module.SpawnSpec(argv=[]))
    rpc._proc = SimpleNamespace(stdout=reader, returncode=0, pid=0)
    rpc._expected_exit = True
    dispatched = 0
    opportunities: list[int] = []

    def on_event(event: dict[str, Any]) -> None:
        nonlocal dispatched
        if event.get("type") == "message_update":
            dispatched += 1

    async def heartbeat() -> None:
        while dispatched < total:
            await asyncio.sleep(0)
            if 0 < dispatched < total:
                opportunities.append(dispatched)

    rpc.on_event(on_event)
    task = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    await rpc._read_stdout()
    elapsed = (time.perf_counter() - started) * 1000
    await task
    return {"frames": dispatched, "elapsed_ms": elapsed,
            "input_opportunities_during_burst": len(opportunities)}


async def _fragmented_transport(module: Any) -> dict[str, Any]:
    # One large frame spans 128 pipe reads. It must not be repeatedly rescanned
    # or recopied while waiting for its final newline. JSON decoding stays real.
    size = 8 * 1024 * 1024
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"type":"event","text":"' + b"x" * size + b'"}\n')
    reader.feed_eof()
    rpc = module.PiRpcProcess(module.SpawnSpec(argv=[]))
    rpc._proc = SimpleNamespace(stdout=reader, returncode=0, pid=0)
    rpc._expected_exit = True
    lengths = []
    rpc.on_event(lambda event: lengths.append(len(event["text"])) if "text" in event else None)
    started = time.perf_counter()
    await rpc._read_stdout()
    elapsed = (time.perf_counter() - started) * 1000
    if lengths != [size]:
        raise RuntimeError("Fragmented RPC frame was corrupted or lost")
    return {"chars": size, "elapsed_ms": elapsed, "exact": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-source", type=Path)
    parser.add_argument("--rpc-source", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    ui = _module(args.chat_source, chat_view, "limbowave.ui._benchmark_chat_snapshot")
    rpc = _module(args.rpc_source, pi_rpc, "limbowave.infrastructure._benchmark_rpc_snapshot")
    app = QApplication.instance() or QApplication([])
    app.setEffectEnabled(Qt.UIEffect.UI_General, False)
    samples = [
        {"single_line": _stream(ui.ChatView, "字", 4_000),
         "multi_line": _stream(ui.ChatView, "One streaming line.\n", 1_000),
         "history": _history(ui.ChatView), "transport": asyncio.run(_transport(rpc)),
         "fragmented_transport": asyncio.run(_fragmented_transport(rpc))}
        for _ in range(args.repeats)
    ]
    summary = {
        key: {metric: statistics.median(sample[key][metric] for sample in samples)
              for metric in ("enqueue_ms", "settled_ms", "document_updates")}
        for key in ("single_line", "multi_line")
    }
    result = {"python": sys.version, "platform": sys.platform,
              "samples": samples, "median": summary,
              "scope": "Synthetic offscreen UI; no provider or real profile"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
