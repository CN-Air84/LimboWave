"""诊断管理器：生命周期、标准 logging 兼容、边界与故障注入。"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from limbowave.infrastructure.diagnostics import (
    LogConfig,
    LogLevel,
    LogManager,
    LogReader,
    LogTargetInUseError,
    log_context,
)


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[LogManager]:
    with LogManager(LogConfig(tmp_path, level="debug", disk_bytes_per_second=0)) as value:
        yield value


def entries(manager: LogManager):
    assert manager.flush()
    return list(LogReader(manager.config).iter_entries())


@pytest.mark.parametrize(
    "level,expected", [("debug", 10), ("crit", 50), ("CRITICAL", 50), (30, 30)]
)
def test_level_aliases(level, expected) -> None:
    assert LogLevel.parse(level) == expected


@pytest.mark.parametrize(
    "options",
    [
        {"name": "../oops"},
        {"level": "trace"},
        {"queue_capacity": 0},
        {"queue_capacity": True},
        {"max_record_bytes": 10},
        {"max_file_bytes": 1000},
        {"backup_count": -1},
        {"flush_interval": 0},
        {"flush_interval": float("nan")},
        {"retry_interval": float("inf")},
        {"disk_bytes_per_second": -1},
    ],
)
def test_invalid_config(tmp_path: Path, options) -> None:
    with pytest.raises(ValueError):
        LogConfig(tmp_path, **options)


def test_lifecycle_is_explicit_and_root_is_untouched(tmp_path: Path) -> None:
    target = tmp_path / "not-created"
    root = logging.getLogger()
    before = (root.level, tuple(root.handlers))
    manager = LogManager(LogConfig(target))
    logger = manager.get_logger("test")
    logger.info("before start")
    assert manager.status().rejected == 1
    assert not target.exists()
    assert manager.start() is manager
    assert manager.start() is manager
    logger.info("running")
    assert manager.close()
    assert manager.close()
    logger.info("after close")
    assert manager.status().rejected == 2
    assert manager.status().state == "closed"
    assert (root.level, tuple(root.handlers)) == before
    with pytest.raises(RuntimeError):
        manager.start()


def test_five_levels_and_crit_source(manager: LogManager) -> None:
    logger = manager.get_logger("test.levels")
    logger.debug("debug %s", "value")
    logger.info("info")
    logger.warning("warning")
    logger.error("error")
    logger.crit("critical")
    result = entries(manager)
    assert [entry.level for entry in result] == list(LogLevel)
    assert result[0].message == "debug value"
    assert result[-1].function == "test_five_levels_and_crit_source"
    assert result[-1].pathname.endswith("test_diagnostics.py")
    assert result[-1].process > 0
    assert result[-1].session_id == manager.session_id


def test_disabled_level_is_lazy_and_level_can_change(manager: LogManager) -> None:
    class Lazy:
        calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "expensive"

    value = Lazy()
    logger = manager.get_logger("test.level")
    manager.set_level("warning")
    logger.debug("%s", value)
    logger.info("%s", value)
    assert value.calls == 0
    manager.set_level("debug")
    logger.debug("%s", value)
    assert value.calls == 1
    assert entries(manager)[0].message == "expensive"


def test_context_precedence_snapshot_and_no_reference_leaks(manager: LogManager) -> None:
    nested = {"items": [1]}
    logger = manager.get_logger("test.context", run_id="bound", nested=nested)
    with log_context(run_id="scope", session_id="s1"):
        logger.bind(component="worker").info("a", extra={"run_id": "call"})
        with log_context(session_id="s2"):
            logger.info("b")
        logger.info("c")
    logger.info("d")
    nested["items"].append(2)
    result = entries(manager)
    assert result[0].context == {
        "run_id": "call",
        "session_id": "s1",
        "nested": {"items": [1]},
        "component": "worker",
    }
    assert result[1].context["session_id"] == "s2"
    assert result[2].context["session_id"] == "s1"
    assert "session_id" not in result[3].context
    copy = manager.recent()[0].context
    copy["nested"]["items"].append(99)
    assert manager.recent()[0].context["nested"] == {"items": [1]}


@pytest.mark.asyncio
async def test_async_context_isolation(manager: LogManager) -> None:
    logger = manager.get_logger("test.async")

    async def work(name: str) -> None:
        with log_context(task=name):
            await asyncio.sleep(0)
            logger.info(name)

    await asyncio.gather(work("one"), work("two"))
    assert {entry.message: entry.context["task"] for entry in entries(manager)} == {
        "one": "one",
        "two": "two",
    }


def test_attach_preserves_existing_handlers_and_record(manager: LogManager) -> None:
    external = logging.Logger("external", level=logging.ERROR)
    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    original = Capture()
    external.addHandler(original)
    with manager.attach(external, level="debug"):
        external.info("value %s", "argument", extra={"run_id": "r1"})
    assert external.handlers == [original]
    assert external.level == logging.ERROR
    assert captured[0].msg == "value %s"
    assert captured[0].args == ("argument",)
    assert entries(manager)[0].context["run_id"] == "r1"


def test_redaction_applies_to_memory_disk_and_console(tmp_path: Path) -> None:
    stream = io.StringIO()
    secret = "sk-live-VERY_SECRET_12345"
    with LogManager(LogConfig(tmp_path, console=True), console_stream=stream) as manager:
        logger = manager.get_logger("test.secret")
        try:
            raise ValueError(f"password=plain-secret Bearer {secret}")
        except ValueError:
            logger.exception(
                "request %s",
                secret,
                extra={
                    "headers": {
                        "Authorization": f"Bearer {secret}",
                        "Cookie": "session=plain-cookie",
                    },
                    "password": "plain-password",
                    "api_key": "arbitrary-api-key",
                },
            )
        result = entries(manager)
        blobs = [
            stream.getvalue(),
            repr(manager.recent()),
            repr(result),
            (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8"),
        ]
        for blob in blobs:
            for forbidden in (
                secret,
                "plain-secret",
                "plain-password",
                "arbitrary-api-key",
                "plain-cookie",
            ):
                assert forbidden not in blob
        assert result[0].exception_type == "ValueError"
        assert "[REDACTED]" in result[0].message
        assert "test_redaction_applies" in result[0].exception


def test_malformed_message_cycles_and_unknown_objects_do_not_escape(manager: LogManager) -> None:
    class Bad:
        def __str__(self) -> str:
            raise ValueError("must not escape")

        def __repr__(self) -> str:
            raise ValueError("must not run")

    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    logger = manager.get_logger("test.safe")
    logger.info("%s", Bad())
    logger.info("valid", extra={"cycle": cycle, "unknown": Bad(), "nan": float("nan")})
    result = entries(manager)
    assert result[0].message == "[unformattable log record]"
    assert result[1].context["cycle"]["self"] == "[cycle]"
    assert result[1].context["unknown"] == "<Bad>"
    assert result[1].context["nan"] == "[non-finite]"
    assert manager.status().format_errors == 1


def test_recursive_message_logging_is_guarded(manager: LogManager) -> None:
    logger = manager.get_logger("test.recursive")

    class Recursive:
        def __str__(self) -> str:
            logger.info("nested")
            return "outer"

    logger.info("%s", Recursive())
    assert [entry.message for entry in entries(manager)] == ["outer"]
    assert manager.status().format_errors == 1


def test_byte_bounds_and_unicode(tmp_path: Path) -> None:
    config = LogConfig(tmp_path, max_record_bytes=1024, disk_bytes_per_second=0)
    with LogManager(config) as manager:
        logger = manager.get_logger("test.中文")
        logger.info("中文消息" * 10000, extra={"data": ["很长" * 5000] * 1000})
        result = entries(manager)
        assert len(result) == 1
        assert result[0].truncated
        assert "中文" in result[0].message
        line = (tmp_path / "diagnostics.jsonl").read_bytes()
        assert len(line) <= 1024
        assert b"\xef\xbf\xbd" not in line


def test_concurrent_producers_batch_on_worker_thread(tmp_path: Path, monkeypatch) -> None:
    config = LogConfig(tmp_path, queue_capacity=4096, recent_capacity=500, disk_bytes_per_second=0)
    with LogManager(config) as manager:
        writer_threads: list[int] = []
        original = manager._sink.write_batch

        def write(payloads: list[bytes]) -> None:
            writer_threads.append(threading.get_ident())
            original(payloads)

        monkeypatch.setattr(manager._sink, "write_batch", write)
        logger = manager.get_logger("test.concurrent")

        def work(number: int) -> None:
            for index in range(100):
                logger.info("%s:%s", number, index)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, range(4)))
        result = entries(manager)
        assert len(result) == len({entry.message for entry in result}) == 400
        assert [entry.sequence for entry in result] == list(range(1, 401))
        assert len(set(writer_threads)) == 1
        assert threading.get_ident() not in writer_threads
        assert manager.status().write_batches < 20
        assert manager.status().queue_dropped == 0


def test_queue_overload_priority_and_close_timeout(tmp_path: Path, monkeypatch) -> None:
    config = LogConfig(
        tmp_path, level="debug", queue_capacity=2, batch_size=1, disk_bytes_per_second=0
    )
    manager = LogManager(config)
    entered, release = threading.Event(), threading.Event()
    original = manager._sink.write_batch

    def slow(payloads: list[bytes]) -> None:
        entered.set()
        assert release.wait(5)
        original(payloads)

    monkeypatch.setattr(manager._sink, "write_batch", slow)
    manager.start()
    try:
        logger = manager.get_logger("test.queue")
        logger.info("in flight")
        assert entered.wait(3)
        logger.debug("evict debug")
        logger.info("evict info")
        logger.error("keep error")
        logger.crit("keep critical")
        logger.warning("drop warning")
        assert manager.status().pending == 3
        assert manager.status().queue_dropped == 3
        assert manager.status().dropped_by_level["WARNING"] == 1
        assert not manager.flush(timeout=0.01)
        assert not manager.close(timeout=0.01)
        assert manager.status().state == "closing"
    finally:
        release.set()
        assert manager.close()
    assert [entry.message for entry in entries(manager)] == [
        "in flight",
        "keep error",
        "keep critical",
    ]
    assert manager.status().pending == 0


def test_disk_rate_limiter_keeps_highest_level_in_batch(tmp_path: Path) -> None:
    config = LogConfig(
        tmp_path, max_record_bytes=1024, disk_bytes_per_second=1, batch_size=100, flush_interval=60
    )
    with LogManager(config) as manager:
        logger = manager.get_logger("test.rate")
        for _ in range(4):
            logger.info("low " + "x" * 400)
        logger.crit("high " + "x" * 400)
        result = entries(manager)
        assert [entry.level for entry in result] == [LogLevel.CRITICAL]
        assert manager.status().rate_limited == 4
        assert manager.status().bytes_written <= 1024
        assert len(manager.recent()) == 5


@pytest.mark.parametrize("backups", [0, 2])
def test_rotation_bounds_and_foreign_files(tmp_path: Path, backups: int) -> None:
    foreign = tmp_path / "not-our-log.jsonl"
    foreign.write_text("keep", encoding="utf-8")
    config = LogConfig(
        tmp_path,
        max_file_bytes=1200,
        max_record_bytes=1024,
        backup_count=backups,
        disk_bytes_per_second=0,
    )
    with LogManager(config) as manager:
        logger = manager.get_logger("test.rotate")
        for index in range(8):
            logger.info("%s %s", index, "x" * 300)
        result = entries(manager)
        files = [path for path in LogReader(config).paths() if path.exists()]
        assert len(files) == backups + 1
        assert all(path.stat().st_size <= 1200 for path in files)
        assert result[-1].sequence == 8
        assert len(result) == backups + 1
        assert foreign.read_text(encoding="utf-8") == "keep"


def test_disk_failure_backoff_and_recovery(manager: LogManager, monkeypatch) -> None:
    attempts = 0
    original = manager._sink.write_batch

    def fail(payloads: list[bytes]) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("password=do-not-leak")

    monkeypatch.setattr(manager._sink, "write_batch", fail)
    logger = manager.get_logger("test.disk")
    logger.error("lost one")
    assert manager.flush()
    assert manager.status().file_dropped == 1
    assert "do-not-leak" not in (manager.status().last_file_error or "")
    logger.error("lost two during backoff")
    assert manager.flush()
    assert attempts == 1
    assert manager.status().file_dropped == 2
    monkeypatch.setattr(manager._sink, "write_batch", original)
    manager._retry_at = 0
    logger.info("recovered")
    assert [entry.message for entry in entries(manager)] == ["recovered"]
    assert manager.status().last_file_error is None
    assert manager.status().dropped_by_level["ERROR"] == 2


def test_console_failure_does_not_prevent_file_output(tmp_path: Path) -> None:
    class BrokenConsole(io.StringIO):
        def write(self, value: str) -> int:
            raise OSError("console unavailable")

    with LogManager(LogConfig(tmp_path, console=True), console_stream=BrokenConsole()) as manager:
        manager.get_logger("test.console").info("still written")
        assert len(entries(manager)) == 1
        assert manager.status().console_errors == 1


def test_target_exclusivity_and_release(manager: LogManager) -> None:
    other = LogManager(manager.config)
    with pytest.raises(LogTargetInUseError):
        other.start()
    assert manager.close()
    with other:
        other.get_logger("test.lock").info("acquired after release")
        assert len(entries(other)) == 1


def test_memory_ring_is_bounded_and_not_disk_history(tmp_path: Path) -> None:
    with LogManager(LogConfig(tmp_path, recent_capacity=3, disk_bytes_per_second=0)) as manager:
        logger = manager.get_logger("test.ring")
        for index in range(8):
            logger.info("%d", index)
        assert [entry.sequence for entry in manager.recent(limit=2)] == [7, 8]
        assert [entry.sequence for entry in manager.recent(after_sequence=7)] == [8]
        assert len(entries(manager)) == 8
        with pytest.raises(ValueError):
            manager.recent(limit=0)


def test_startup_bad_directory_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_text("not a directory", encoding="utf-8")
    manager = LogManager(LogConfig(path))
    with pytest.raises(OSError):
        manager.start()
    assert manager.status().state == "new"
    assert manager.close()


def test_partial_tail_is_isolated_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.jsonl"
    path.write_bytes(b'{"version":1,"message":"truncated')
    with LogManager(LogConfig(tmp_path)) as manager:
        manager.get_logger("test.tail").info("complete new record")
        assert manager.flush()
        reader = LogReader(manager.config)
        assert [entry.message for entry in reader.iter_entries()] == ["complete new record"]
        assert reader.stats.invalid_lines == 1


def test_close_drains_without_explicit_flush(tmp_path: Path) -> None:
    config = replace(LogConfig(tmp_path), batch_size=100, flush_interval=60)
    with LogManager(config) as manager:
        for index in range(5):
            manager.get_logger("test.shutdown").info("%s", index)
    assert len(list(LogReader(config).iter_entries())) == 5
