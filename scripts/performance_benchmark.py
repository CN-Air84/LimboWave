"""Reproducible startup/first-use probe; never opens the user's vault or a listener.

Run with the project Python: scripts/performance_benchmark.py --samples 5
--include-settings --history-intents 2000 --output .var/performance.json

Every sample uses a fresh process, offscreen Qt, temporary encrypted storage,
a test-only cheap KDF, and stubbed shell/model discovery. Times are synthetic
stage measurements, NOT OS-cold-launch latency or an FPS measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def working_set_mb() -> float | None:
    """Current process working set (not total memory of its child processes)."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb,
    ):
        return None
    return round(counters.WorkingSetSize / 1024**2, 2)


def summarize(samples: list[dict[str, Any]]) -> dict[str, float]:
    if not samples:
        return {}
    return {
        key: round(statistics.median(sample[key] for sample in samples), 3)
        for key, value in samples[0].items()
        if type(value) in (int, float)
        and all(type(sample.get(key)) in (int, float) for sample in samples)
    }


def startup_sample(*, include_settings: bool) -> dict[str, Any]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(ROOT / "src"))
    started = time.perf_counter()
    from limbowave import app

    result: dict[str, Any] = {"import_ms": (time.perf_counter() - started) * 1000}
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget
    from qasync import QEventLoop

    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure import shell
    from limbowave.infrastructure.crypto.vault import KdfParams, Vault

    # Deliberately exclude external runtime/shell discovery and real credentials.
    shell.probe_shell = lambda *args: None
    app._build_kernel = lambda *args, **kwargs: None
    started = time.perf_counter()
    qt = app.build_application(["performance-benchmark"])
    window = app.MainWindow()
    window.show_full_page(app.LoginPage())
    window.show()
    qt.processEvents()
    result["window_ms"] = (time.perf_counter() - started) * 1000
    result["login_rss_mb"] = working_set_mb()
    loop = QEventLoop(qt)
    asyncio.set_event_loop(loop)
    try:
        with tempfile.TemporaryDirectory(prefix="limbowave-performance-") as temporary:
            root = Path(temporary)
            key = Vault(root / "vault.json", params=KdfParams(
                time_cost=1, memory_cost=8, parallelism=1,
            )).create("synthetic-benchmark-only")
            pulses: list[float] = []
            timer = QTimer(window)
            timer.setInterval(10)
            timer.timeout.connect(lambda: pulses.append(time.perf_counter()))
            timer.start()
            shutdown = None
            try:
                started = time.perf_counter()
                _, _, settings, finish, shutdown = app._wire(
                    window, AppPaths(root, root / "logs"), key, appearance_prepared=True,
                )
                result["wire_ms"] = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                loop.run_until_complete(finish())
                result["finish_ms"] = (time.perf_counter() - started) * 1000
                result["ready_rss_mb"] = working_set_mb()
                result["widgets_before_settings"] = len(window.findChildren(QWidget))
                result["web_imported"] = "fastapi" in sys.modules
                result["pinyin_imported"] = "pypinyin" in sys.modules
                result["markdown_imported"] = "markdown_it" in sys.modules
                # Include the final synchronous stage in the heartbeat observation.
                loop.run_until_complete(asyncio.sleep(0.03))
                result["heartbeat_max_gap_ms"] = max(
                    [0.0] + [(right - left) * 1000 for left, right in pairwise(pulses)]
                )
                timer.stop()
                if include_settings:
                    started = time.perf_counter()
                    settings()
                    result["settings_ms"] = (time.perf_counter() - started) * 1000
                    result["warm_rss_mb"] = working_set_mb()
                    result["widgets_after_settings"] = len(window.findChildren(QWidget))
            finally:
                timer.stop()
                if shutdown is not None:
                    loop.run_until_complete(shutdown())
    finally:
        window.close()
        loop.close()
        asyncio.set_event_loop(None)
    return result


def history_sample(count: int) -> dict[str, Any]:
    """Compare legacy and scoped lookup on the exact same synthetic database."""
    from datetime import UTC, datetime, timedelta

    sys.path.insert(0, str(ROOT / "src"))
    from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
    from limbowave.domain.run import RunRecord, RunStatus
    from limbowave.domain.snapshots import RequestIntentSnapshot
    from limbowave.infrastructure.crypto.vault import VaultKey
    from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

    with tempfile.TemporaryDirectory(prefix="limbowave-history-performance-") as temporary:
        factory = sqlite_uow_factory(Path(temporary) / "history.db", VaultKey(os.urandom(32)))
        now = datetime.now(UTC)
        selected: list[str] = []
        try:
            with factory() as uow:
                for index in range(50):
                    cid = f"conversation-{index}"
                    uow.conversations.add(Conversation(cid, cid, now))
                    uow.branches.add(Branch(cid, cid, now))
                for index in range(count):
                    cid = f"conversation-{index % 50}"
                    mid, rid = f"message-{index}", f"run-{index}"
                    stamp = now + timedelta(seconds=index)
                    uow.messages.add(Message(
                        mid, cid, cid, MessageRole.USER, "benchmark", stamp,
                    ))
                    uow.runs.add(RunRecord(rid, cid, cid, RunStatus.COMPLETED, stamp, mid))
                    uow.snapshots.add_intent(RequestIntentSnapshot(
                        f"intent-{index}", rid, cid, cid, "model", "endpoint", "test", stamp,
                        message_ids=(mid,), attachment_ids=(f"file-{index}",),
                        app_params={"synthetic_context": "x" * 4096},
                    ))
                    if index % 50 == 0:
                        selected.append(mid)
                uow.commit()
            legacy_ms: list[float] = []
            scoped_ms: list[float] = []
            wanted = set(selected)
            for _ in range(3):
                with factory() as uow:
                    started = time.perf_counter()
                    legacy = {
                        intent.message_ids[-1]: intent.attachment_ids
                        for intent in uow.snapshots.list_all_intents()
                        if intent.message_ids and intent.message_ids[-1] in wanted
                    }
                    legacy_ms.append((time.perf_counter() - started) * 1000)
                    started = time.perf_counter()
                    scoped = uow.snapshots.attachment_ids_for_messages("conversation-0", selected)
                    scoped_ms.append((time.perf_counter() - started) * 1000)
                if legacy != scoped:
                    raise AssertionError("scoped lookup changed attachment associations")
            return {
                "intents": count, "selected_messages": len(selected),
                "legacy_ms": statistics.median(legacy_ms),
                "scoped_ms": statistics.median(scoped_ms),
            }
        finally:
            factory.close()


def positive_count(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=positive_count, default=3)
    parser.add_argument("--include-settings", action="store_true")
    parser.add_argument("--history-intents", type=positive_count)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        print("RESULT " + json.dumps(startup_sample(include_settings=args.include_settings)))
        return 0
    samples = []
    for _ in range(args.samples):
        command = [sys.executable, str(Path(__file__).resolve()), "--child"]
        if args.include_settings:
            command.append("--include-settings")
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120, check=True,
        )
        line = next(line for line in completed.stdout.splitlines() if line.startswith("RESULT "))
        samples.append(json.loads(line.removeprefix("RESULT ")))
    report = {
        "method": "fresh process, offscreen Qt, temporary data; no real KDF/shell/model/listener",
        "python": sys.version.split()[0], "platform": sys.platform,
        "samples": samples, "median": summarize(samples),
    }
    if args.history_intents:
        report["history"] = history_sample(args.history_intents)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    raise SystemExit(main())
