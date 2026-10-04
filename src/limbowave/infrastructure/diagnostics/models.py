"""运行诊断的数据契约；不依赖 Qt，也不触发日志初始化。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any


class LogLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
    CRIT = 50

    @classmethod
    def parse(cls, value: LogLevel | str | int) -> LogLevel:
        if isinstance(value, str):
            try:
                return cls[value.upper()]
            except KeyError as exc:
                raise ValueError(f"Unknown log level: {value}") from exc
        return cls(value)

    @classmethod
    def from_logging(cls, value: int) -> LogLevel:
        """外部 logging 的自定义数字等级归入最近的下档，最高封顶 CRITICAL。"""
        return next((level for level in reversed(list(cls)) if value >= level), cls.DEBUG)


@dataclass(frozen=True, slots=True)
class LogConfig:
    directory: Path
    name: str = "diagnostics"
    level: LogLevel | str | int = LogLevel.INFO
    queue_capacity: int = 2048
    recent_capacity: int = 1000
    batch_size: int = 64
    flush_interval: float = 0.5
    max_file_bytes: int = 5 * 1024 * 1024
    backup_count: int = 4
    max_record_bytes: int = 16 * 1024
    disk_bytes_per_second: int = 64 * 1024
    retry_interval: float = 5.0
    console: bool = False
    file_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "level", LogLevel.parse(self.level))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.name):
            raise ValueError("Log name must be a simple filename stem")
        for name in ("queue_capacity", "recent_capacity", "batch_size", "max_file_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("backup_count", "disk_bytes_per_second"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(self.max_record_bytes) is not int or self.max_record_bytes < 1024:
            raise ValueError("max_record_bytes must be at least 1024")
        if self.max_record_bytes > self.max_file_bytes:
            raise ValueError("A record must fit in one log file")
        for name in ("flush_interval", "retry_interval"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class LogEntry:
    sequence: int
    timestamp: str
    level: LogLevel
    logger: str
    message: str
    pathname: str = ""
    lineno: int = 0
    function: str = ""
    process: int = 0
    thread: str = ""
    exception: str = ""
    exception_type: str = ""
    context_json: str = "{}"
    truncated: bool = False
    session_id: str = ""

    @property
    def context(self) -> dict[str, Any]:
        # 每次返回独立副本：查看器不能修改队列/环形缓冲里的记录。
        value: dict[str, Any] = json.loads(self.context_json)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "level": self.level.name,
            "logger": self.logger,
            "message": self.message,
            "pathname": self.pathname,
            "lineno": self.lineno,
            "function": self.function,
            "process": self.process,
            "thread": self.thread,
            "exception": self.exception,
            "exception_type": self.exception_type,
            "context": self.context,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class LogStatus:
    state: str
    sequence: int
    queued: int
    pending: int
    written: int
    bytes_written: int
    write_batches: int
    queue_dropped: int
    rate_limited: int
    file_dropped: int
    rejected: int
    format_errors: int
    console_errors: int
    dropped_by_level: dict[str, int]
    last_file_error: str | None
    last_console_error: str | None
    file_skipped: int = 0
