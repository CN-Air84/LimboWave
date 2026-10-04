"""日志核心边界回归；所有写入目标均为 pytest 临时目录。"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from limbowave.infrastructure.diagnostics import LogConfig, LogManager, LogReader
from limbowave.infrastructure.diagnostics.sanitize import safe_text


def test_core_import_does_not_load_qt_or_start_threads() -> None:
    code = (
        "import sys, threading, logging; before = threading.active_count(); "
        "import limbowave.infrastructure.diagnostics; "
        "assert threading.active_count() == before; "
        "assert not any(n.startswith('PySide6') for n in sys.modules); "
        "assert not logging.getLogger().handlers"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=15)


@pytest.mark.parametrize(
    "text",
    [
        'Authorization="Bearer arbitrary-token"',
        "{'Cookie': 'session=arbitrary-token'}",
        "https://user:arbitrary-token@example.invalid/path",
        "password='arbitrary-token'",
        "api_key=arbitrary-token&safe=1",
    ],
)
def test_common_free_text_credentials(text: str) -> None:
    assert "arbitrary-token" not in safe_text(text)


def test_truncation_flag_for_text_and_containers(tmp_path: Path) -> None:
    with LogManager(LogConfig(tmp_path, disk_bytes_per_second=0)) as manager:
        logger = manager.get_logger("edge")
        logger.info("x" * 5000)
        logger.info("container", extra={"items": list(range(1000))})
        assert manager.flush()
        result = manager.recent()
        assert all(entry.truncated for entry in result)
        assert result[1].context["items"][-1] == "[truncated]"


def test_external_numeric_metadata_is_bounded(tmp_path: Path) -> None:
    with LogManager(LogConfig(tmp_path, max_record_bytes=1024)) as manager:
        record = logging.LogRecord("external", 20, "source", 10**10000, "normal", (), None)
        record.process = 10**10000
        manager.handler.handle(record)
        assert manager.flush()
        result = list(LogReader(manager.config).iter_entries())
        assert len(result) == 1
        assert result[0].lineno == result[0].process == 0


def test_low_volume_is_flushed_by_timer_and_idle_does_not_write(
    tmp_path: Path, monkeypatch
) -> None:
    with LogManager(LogConfig(tmp_path, batch_size=100, flush_interval=0.02)) as manager:
        wrote = threading.Event()
        original = manager._sink.write_batch

        def write(payloads: list[bytes]) -> None:
            original(payloads)
            wrote.set()

        monkeypatch.setattr(manager._sink, "write_batch", write)
        manager.get_logger("edge").info("without manual flush")
        assert wrote.wait(3)
        assert manager.flush()
        wrote.clear()
        assert not wrote.wait(0.06)
        assert manager.status().write_batches == 1


def test_thread_start_failure_releases_target_and_can_close(tmp_path: Path, monkeypatch) -> None:
    config = LogConfig(tmp_path)
    manager = LogManager(config)

    def fail(thread: threading.Thread) -> None:
        raise RuntimeError("cannot start")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="cannot start"):
            manager.start()
    assert manager.close()
    assert manager.status().state == "failed"
    with LogManager(config) as other:
        other.get_logger("edge").info("lock released")
        assert other.flush()


def test_partial_batch_failure_counts_only_unsaved_records(tmp_path: Path, monkeypatch) -> None:
    config = LogConfig(
        tmp_path,
        max_file_bytes=1200,
        max_record_bytes=1024,
        batch_size=100,
        flush_interval=60,
        disk_bytes_per_second=0,
    )
    with LogManager(config) as manager:
        calls = 0
        original = manager._sink._write

        def fail_second(chunk: bytearray, count: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            original(chunk, count)

        monkeypatch.setattr(manager._sink, "_write", fail_second)
        logger = manager.get_logger("edge")
        for _ in range(3):
            logger.error("x" * 400)
        assert manager.flush()
        assert manager.status().written == 1
        assert manager.status().file_dropped == 2
        assert manager.status().dropped_by_level["ERROR"] == 2
        assert len(list(LogReader(config).iter_entries())) == 1


def test_flush_fails_promptly_if_writer_crashes(tmp_path: Path, monkeypatch) -> None:
    manager = LogManager(LogConfig(tmp_path))

    def crash(batch) -> None:
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(manager, "_process_batch", crash)
    manager.start()
    try:
        manager.get_logger("edge").info("not processed")
        assert not manager.flush(timeout=3)
        assert manager.status().state == "failed"
        assert "Writer stopped" in (manager.status().last_file_error or "")
    finally:
        assert manager.close()
