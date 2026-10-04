"""独立的查询、有限内存流式读取与辅助统计；不执行外部模型或网络请求。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from limbowave.infrastructure.diagnostics.models import LogConfig, LogEntry, LogLevel
from limbowave.infrastructure.diagnostics.sanitize import safe_context, safe_text


@dataclass(frozen=True, slots=True)
class LogQuery:
    min_level: LogLevel | str | int = LogLevel.DEBUG
    levels: frozenset[LogLevel] | None = None
    text: str = ""
    logger: str = ""
    since: datetime | None = None
    until: datetime | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_level", LogLevel.parse(self.min_level))
        for value in (self.since, self.until):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Query timestamps must be timezone-aware")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError("since must not exceed until")

    def matches(self, entry: LogEntry) -> bool:
        if entry.level < LogLevel.parse(self.min_level):
            return False
        if self.levels is not None and entry.level not in self.levels:
            return False
        if self.logger and not (
            entry.logger == self.logger or entry.logger.startswith(self.logger + ".")
        ):
            return False
        if self.since is not None or self.until is not None:
            when = datetime.fromisoformat(entry.timestamp)
            if self.since is not None and when < self.since:
                return False
            if self.until is not None and when > self.until:
                return False
        if self.context:
            fields = entry.context
            if any(
                key not in fields or fields[key] != value for key, value in self.context.items()
            ):
                return False
        return (
            not self.text
            or self.text.casefold()
            in "\n".join(
                (entry.logger, entry.message, entry.exception, entry.context_json)
            ).casefold()
        )


@dataclass(frozen=True, slots=True)
class ReadStats:
    files_read: int = 0
    records_read: int = 0
    invalid_lines: int = 0
    oversized_lines: int = 0
    unreadable_files: int = 0


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _decode(line: bytes) -> LogEntry:
    value = json.loads(line, parse_constant=_reject_constant)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Unsupported log schema")
    for key in ("timestamp", "level", "logger", "message"):
        if not isinstance(value.get(key), str):
            raise ValueError(f"Missing string field: {key}")
    for key in ("session_id", "pathname", "function", "thread", "exception", "exception_type"):
        if not isinstance(value.get(key, ""), str):
            raise ValueError(f"Invalid string field: {key}")
    for key in ("sequence", "lineno", "process"):
        number = value.get(key, 0)
        if type(number) is not int or not 0 <= number < 2**63:
            raise ValueError(f"Invalid integer field: {key}")
    if not isinstance(value.get("context", {}), dict):
        raise ValueError("context must be an object")
    timestamp = datetime.fromisoformat(value["timestamp"])
    if timestamp.utcoffset() is None:
        raise ValueError("Log timestamp must be timezone-aware")
    return LogEntry(
        sequence=value["sequence"],
        timestamp=timestamp.astimezone(UTC).isoformat(timespec="milliseconds"),
        level=LogLevel.parse(value["level"]),
        logger=safe_text(value["logger"], 128),
        message=safe_text(value["message"]),
        pathname=safe_text(value.get("pathname", ""), 256),
        lineno=value.get("lineno", 0),
        function=safe_text(value.get("function", ""), 128),
        process=value.get("process", 0),
        thread=safe_text(value.get("thread", ""), 128),
        exception=safe_text(value.get("exception", ""), 8192),
        exception_type=safe_text(value.get("exception_type", ""), 128),
        context_json=safe_context(value.get("context", {})),
        truncated=bool(value.get("truncated", False)),
        session_id=safe_text(value.get("session_id", ""), 64),
    )


class LogReader:
    """一次只迭代一个扫描；stats 随迭代更新。在线轮转期间不承诺一致性快照。"""

    def __init__(self, config: LogConfig) -> None:
        self.config = config
        self.stats = ReadStats()

    def paths(self) -> list[Path]:
        active = self.config.directory / f"{self.config.name}.jsonl"
        return [Path(f"{active}.{n}") for n in range(self.config.backup_count, 0, -1)] + [active]

    def iter_entries(
        self, query: LogQuery | None = None, *, limit: int | None = None
    ) -> Iterator[LogEntry]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        self.stats = ReadStats()
        yielded = 0
        for path in self.paths():
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                continue
            except OSError:
                self.stats = replace(self.stats, unreadable_files=self.stats.unreadable_files + 1)
                continue
            self.stats = replace(self.stats, files_read=self.stats.files_read + 1)
            with stream:
                try:
                    while line := stream.readline(self.config.max_record_bytes + 1):
                        if len(line) > self.config.max_record_bytes:
                            while line and not line.endswith(b"\n"):
                                line = stream.readline(self.config.max_record_bytes + 1)
                            self.stats = replace(
                                self.stats, oversized_lines=self.stats.oversized_lines + 1
                            )
                            continue
                        try:
                            if not line.endswith(b"\n"):
                                raise ValueError("Incomplete tail")
                            entry = _decode(line)
                        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                            self.stats = replace(
                                self.stats, invalid_lines=self.stats.invalid_lines + 1
                            )
                            continue
                        self.stats = replace(self.stats, records_read=self.stats.records_read + 1)
                        if query is None or query.matches(entry):
                            yield entry
                            yielded += 1
                            if limit is not None and yielded >= limit:
                                return
                except OSError:
                    self.stats = replace(
                        self.stats, unreadable_files=self.stats.unreadable_files + 1
                    )


@dataclass(frozen=True, slots=True)
class ProblemGroup:
    level: LogLevel
    logger: str
    message: str
    exception_type: str
    count: int


@dataclass(frozen=True, slots=True)
class LogSummary:
    total: int
    by_level: dict[str, int]
    by_logger: dict[str, int]
    by_exception: dict[str, int]
    repeated_problems: tuple[ProblemGroup, ...]
    first_timestamp: str | None
    last_timestamp: str | None
    groups_truncated: bool


class LogAnalyzer:
    @staticmethod
    def summarize(
        entries: Iterable[LogEntry], *, top: int = 10, max_groups: int = 200
    ) -> LogSummary:
        if top < 1 or max_groups < 1:
            raise ValueError("top and max_groups must be positive")
        levels: Counter[str] = Counter()
        loggers: Counter[str] = Counter()
        exceptions: Counter[str] = Counter()
        problems: Counter[tuple[LogLevel, str, str, str]] = Counter()
        total = 0
        truncated = False
        first: str | None = None
        last: str | None = None

        def count(counter: Counter[str], key: str) -> None:
            nonlocal truncated
            if key not in counter and len(counter) >= max_groups:
                truncated = True
                key = "[other groups]"
            counter[key] += 1

        for entry in entries:
            total += 1
            first = entry.timestamp if first is None else min(first, entry.timestamp)
            last = entry.timestamp if last is None else max(last, entry.timestamp)
            levels[entry.level.name] += 1
            count(loggers, entry.logger)
            if entry.exception_type:
                count(exceptions, entry.exception_type)
            if entry.level >= LogLevel.WARNING:
                key = (entry.level, entry.logger, entry.message, entry.exception_type)
                if key in problems or len(problems) < max_groups:
                    problems[key] += 1
                else:
                    truncated = True
        return LogSummary(
            total=total,
            by_level={level.name: levels[level.name] for level in LogLevel},
            by_logger=dict(loggers.most_common(top)),
            by_exception=dict(exceptions.most_common(top)),
            repeated_problems=tuple(
                ProblemGroup(*key, count) for key, count in problems.most_common(top) if count > 1
            ),
            first_timestamp=first,
            last_timestamp=last,
            groups_truncated=truncated,
        )
