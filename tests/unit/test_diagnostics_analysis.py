"""日志的流式回读、查询窗口及有界统计。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from limbowave.infrastructure.diagnostics import (
    LogAnalyzer,
    LogConfig,
    LogEntry,
    LogLevel,
    LogQuery,
    LogReader,
)

BASE = LogEntry(
    sequence=1,
    timestamp="2026-09-29T12:00:00.000+00:00",
    level=LogLevel.ERROR,
    logger="limbowave.pi.rpc",
    message="网络 timeout",
    exception_type="TimeoutError",
    context_json='{"run_id":"r1"}',
)


def line(entry: LogEntry = BASE) -> bytes:
    return (json.dumps(entry.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")


@pytest.mark.parametrize(
    "query,expected",
    [
        (LogQuery(min_level="error"), True),
        (LogQuery(min_level="crit"), False),
        (LogQuery(levels=frozenset()), False),
        (LogQuery(levels=frozenset({LogLevel.ERROR})), True),
        (LogQuery(text="TIMEOUT"), True),
        (LogQuery(text="网络"), True),
        (LogQuery(text="missing"), False),
        (LogQuery(logger="limbowave.pi"), True),
        (LogQuery(logger="limbowave.p"), False),
        (LogQuery(context={"run_id": "r1"}), True),
        (LogQuery(context={"absent": None}), False),
        (LogQuery(since=datetime(2026, 9, 29, 12, tzinfo=UTC)), True),
        (LogQuery(until=datetime(2026, 9, 29, 11, tzinfo=UTC)), False),
        (LogQuery(since=datetime(2026, 9, 29, 13, tzinfo=UTC)), False),
    ],
)
def test_query(query: LogQuery, expected: bool) -> None:
    assert query.matches(BASE) == expected


def test_query_requires_aware_ordered_dates() -> None:
    with pytest.raises(ValueError):
        LogQuery(since=datetime(2026, 1, 1))
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        LogQuery(since=now, until=now - timedelta(seconds=1))


def test_reader_skips_invalid_large_and_truncated_lines(tmp_path: Path) -> None:
    config = LogConfig(tmp_path, max_record_bytes=1024)
    path = tmp_path / "diagnostics.jsonl"
    path.write_bytes(
        b"not json\n"
        + b"\xff\n"
        + b"x" * 3000
        + b"\n"
        + b"[]\n"
        + line()
        + line(replace(BASE, sequence=2))[:-1]
    )
    reader = LogReader(config)
    result = list(reader.iter_entries())
    assert result == [BASE]
    assert reader.stats.invalid_lines == 4
    assert reader.stats.oversized_lines == 1
    assert reader.stats.records_read == 1
    assert reader.stats.files_read == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"version": 2},
        {"timestamp": "invalid"},
        {"timestamp": "2026-09-29"},
        {"level": "trace"},
        {"context": []},
        {"lineno": True},
        {"sequence": -1},
        {"message": []},
        {"thread": 9},
        {"context": {"value": float("nan")}},
    ],
)
def test_reader_validates_schema(tmp_path: Path, patch) -> None:
    value = BASE.to_dict() | patch
    path = tmp_path / "diagnostics.jsonl"
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    reader = LogReader(LogConfig(tmp_path))
    assert list(reader.iter_entries()) == []
    assert reader.stats.invalid_lines == 1


def test_reader_uses_oldest_backup_first_and_limits_matches(tmp_path: Path) -> None:
    config = LogConfig(tmp_path, backup_count=2)
    (tmp_path / "diagnostics.jsonl.2").write_bytes(line(replace(BASE, sequence=1)))
    (tmp_path / "diagnostics.jsonl.1").write_bytes(
        line(replace(BASE, sequence=2, level=LogLevel.INFO))
    )
    (tmp_path / "diagnostics.jsonl").write_bytes(line(replace(BASE, sequence=3)))
    reader = LogReader(config)
    assert [entry.sequence for entry in reader.iter_entries()] == [1, 2, 3]
    assert [
        entry.sequence for entry in reader.iter_entries(LogQuery(min_level="error"), limit=1)
    ] == [1]
    assert reader.stats.files_read == 1
    with pytest.raises(ValueError):
        list(reader.iter_entries(limit=0))


def test_reader_redacts_external_fields_again(tmp_path: Path) -> None:
    value = replace(BASE, message="password=plain-secret", context_json='{"token":"arbitrary"}')
    (tmp_path / "diagnostics.jsonl").write_bytes(line(value))
    entry = next(LogReader(LogConfig(tmp_path)).iter_entries())
    assert "plain-secret" not in entry.message
    assert entry.context["token"] == "[REDACTED]"


def test_missing_directory_has_empty_scan(tmp_path: Path) -> None:
    reader = LogReader(LogConfig(tmp_path / "missing"))
    assert list(reader.iter_entries()) == []
    assert reader.stats.files_read == 0
    assert not reader.config.directory.exists()


def test_summary_counts_exact_groups_and_empty_window() -> None:
    summary = LogAnalyzer.summarize(
        [
            BASE,
            replace(BASE, sequence=2),
            replace(BASE, level=LogLevel.INFO, exception_type=""),
        ]
    )
    assert summary.total == 3
    assert summary.by_level == {"DEBUG": 0, "INFO": 1, "WARNING": 0, "ERROR": 2, "CRITICAL": 0}
    assert summary.by_exception == {"TimeoutError": 2}
    assert summary.repeated_problems[0].count == 2
    assert summary.first_timestamp == summary.last_timestamp == BASE.timestamp
    empty = LogAnalyzer.summarize([])
    assert empty.total == 0
    assert empty.first_timestamp is None
    assert not empty.repeated_problems


def test_summary_group_cardinality_is_bounded() -> None:
    summary = LogAnalyzer.summarize(
        (
            replace(BASE, logger=f"module.{n}", message=f"unique {n}", exception_type=f"Error{n}")
            for n in range(1000)
        ),
        max_groups=3,
        top=100,
    )
    assert summary.total == 1000
    assert summary.groups_truncated
    assert len(summary.by_logger) <= 4
    assert len(summary.by_exception) <= 4
    assert len(summary.repeated_problems) <= 3
