"""显式生命周期的诊断管理器；不安装全局日志或异常钩子。"""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from itertools import islice
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TextIO

from limbowave.infrastructure.diagnostics.models import LogConfig, LogEntry, LogLevel, LogStatus
from limbowave.infrastructure.diagnostics.sanitize import encode_entry, safe_text, snapshot_record
from limbowave.infrastructure.diagnostics.storage import RotatingSink

if TYPE_CHECKING:
    from limbowave.infrastructure.diagnostics.analysis import LogQuery

_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "diagnostic_context", default=MappingProxyType({})
)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """任务局部上下文，支持嵌套；新线程须显式 copy_context 或重新绑定。"""
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class DiagnosticLogger(logging.LoggerAdapter[logging.Logger]):
    def bind(self, **fields: Any) -> DiagnosticLogger:
        return DiagnosticLogger(self.logger, {**(self.extra or {}), **fields})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        fields = dict(self.extra or {})
        extra = kwargs.get("extra", {})
        if isinstance(extra, Mapping):
            fields.update(islice(extra.items(), 64))
        kwargs["extra"] = {"context": fields}
        return msg, kwargs

    def crit(self, msg: object, *args: object, **kwargs: Any) -> None:
        kwargs["stacklevel"] = kwargs.get("stacklevel", 1) + 1
        self.critical(msg, *args, **kwargs)


class DiagnosticHandler(logging.Handler):
    """可显式添加到现有 logging.Logger；不改写原始 LogRecord。"""

    def __init__(self, owner: LogManager) -> None:
        super().__init__(int(LogLevel.parse(owner.config.level)))
        self._owner = owner
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            self._owner._format_error()
            return
        self._local.active = True
        try:
            self._owner._submit(record)
        finally:
            self._local.active = False


class LogManager:
    def __init__(self, config: LogConfig, *, console_stream: TextIO | None = None) -> None:
        self.config = config
        self.session_id = uuid.uuid4().hex
        self.handler = DiagnosticHandler(self)
        self._logging = logging.Manager(logging.RootLogger(logging.NOTSET))
        self._level = LogLevel.parse(config.level)
        self._condition = threading.Condition()
        self._queue: deque[tuple[LogEntry, bytes]] = deque()
        self._queued_levels: Counter[LogLevel] = Counter()
        self._recent: deque[LogEntry] = deque(maxlen=config.recent_capacity)
        self._sink = RotatingSink(config)
        self._console = console_stream if console_stream is not None else sys.stderr
        self._thread: threading.Thread | None = None
        self._state = "new"
        self._sequence = self._last_queued = self._processed = self._flush_target = 0
        self._inflight = 0
        self._counts: Counter[str] = Counter()
        self._dropped: Counter[str] = Counter()
        self._last_file_error: str | None = None
        self._last_console_error: str | None = None
        self._retry_at = 0.0
        self._bucket_capacity = max(config.disk_bytes_per_second, config.max_record_bytes)
        self._tokens = float(self._bucket_capacity)
        self._token_time = time.monotonic()

    def start(self) -> LogManager:
        """幂等启动；目录/锁错误在启动时抛出。关闭后不得复用此实例。"""
        with self._condition:
            if self._state == "running":
                return self
            if self._state != "new":
                raise RuntimeError("A stopped LogManager cannot be restarted")
            if self.config.file_enabled:
                self._sink.acquire()
            self._state = "running"
            try:
                self._thread = threading.Thread(
                    target=self._run, name=f"diagnostics-{self.config.name}", daemon=True
                )
                self._thread.start()
            except Exception:
                self._state = "failed"
                self._thread = None
                self._sink.close()
                raise
        return self

    def __enter__(self) -> LogManager:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        if not self.close():
            raise TimeoutError("Diagnostic writer did not stop; target lock is still held")

    def get_logger(self, name: str, **fields: Any) -> DiagnosticLogger:
        with self._condition:
            logger = self._logging.getLogger(name)
            logger.setLevel(int(self._level))
            logger.propagate = False
            logger.addHandler(self.handler)
        return DiagnosticLogger(logger, fields)

    @property
    def level(self) -> LogLevel:
        with self._condition:
            return self._level

    def set_level(self, level: LogLevel | str | int) -> None:
        parsed = LogLevel.parse(level)
        with self._condition:
            self._level = parsed
            self.handler.setLevel(int(parsed))
            for logger in self._logging.loggerDict.values():
                if isinstance(logger, logging.Logger):
                    logger.setLevel(int(parsed))

    @contextmanager
    def attach(
        self, logger: logging.Logger, *, level: LogLevel | str | int | None = None
    ) -> Iterator[None]:
        """仅显式接线；保留原处理器，退出时恢复本方法调整的等级。"""
        old_level = logger.level
        present = self.handler in logger.handlers
        if level is not None:
            logger.setLevel(int(LogLevel.parse(level)))
        logger.addHandler(self.handler)
        try:
            yield
        finally:
            if not present:
                logger.removeHandler(self.handler)
            if level is not None:
                logger.setLevel(old_level)

    def _format_error(self) -> None:
        with self._condition:
            self._counts["format_errors"] += 1

    def _submit(self, record: logging.LogRecord) -> None:
        with self._condition:
            if record.levelno < self._level:
                return
            if self._state != "running":
                self._counts["rejected"] += 1
                return
        try:
            entry = snapshot_record(record, _CONTEXT.get(), self.session_id)
        except Exception:
            self._format_error()
            entry = LogEntry(
                sequence=0,
                timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
                level=LogLevel.from_logging(record.levelno),
                logger="diagnostics.format",
                message="[unformattable log record]",
                session_id=self.session_id,
            )
        with self._condition:
            if self._state != "running":
                self._counts["rejected"] += 1
                return
            sequence = self._sequence + 1
            try:
                entry, payload = encode_entry(
                    replace(entry, sequence=sequence), self.config.max_record_bytes
                )
            except Exception:
                self._counts["format_errors"] += 1
                return
            self._sequence = sequence
            self._recent.append(entry)
            if len(self._queue) >= self.config.queue_capacity:
                # 同级洪峰 O(1) 丢弃；只在高等级挤占低等级时才扫描有界队列。
                lowest = next(level for level in LogLevel if self._queued_levels[level])
                self._counts["queue_dropped"] += 1
                if lowest >= entry.level:
                    self._dropped[entry.level.name] += 1
                    return
                index = next(i for i, (item, _) in enumerate(self._queue) if item.level == lowest)
                del self._queue[index]
                self._queued_levels[lowest] -= 1
                self._dropped[lowest.name] += 1
            self._queue.append((entry, payload))
            self._queued_levels[entry.level] += 1
            self._last_queued = sequence
            self._counts["queued"] += 1
            if len(self._queue) == 1 or len(self._queue) >= self.config.batch_size:
                self._condition.notify_all()

    def recent(
        self, query: LogQuery | None = None, *, limit: int = 500, after_sequence: int = 0
    ) -> list[LogEntry]:
        """最近的匹配项，返回时间正序；包含尚未写盘及因过载丢弃的记录。"""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._condition:
            entries = list(self._recent)
        matched: list[LogEntry] = []
        for entry in reversed(entries):
            if entry.sequence > after_sequence and (query is None or query.matches(entry)):
                matched.append(entry)
                if len(matched) >= limit:
                    break
        return list(reversed(matched))

    def status(self) -> LogStatus:
        with self._condition:
            return LogStatus(
                state=self._state,
                sequence=self._sequence,
                pending=len(self._queue) + self._inflight,
                dropped_by_level={level.name: self._dropped[level.name] for level in LogLevel},
                last_file_error=self._last_file_error,
                last_console_error=self._last_console_error,
                **{
                    name: self._counts[name]
                    for name in (
                        "queued",
                        "written",
                        "bytes_written",
                        "write_batches",
                        "queue_dropped",
                        "rate_limited",
                        "file_dropped",
                        "file_skipped",
                        "rejected",
                        "format_errors",
                        "console_errors",
                    )
                },
            )

    def flush(self, timeout: float = 5.0) -> bool:
        """等待调用前入队记录处理完毕；True 不代表未限流或已 fsync，须检查 status。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            target = self._last_queued
            self._flush_target = max(self._flush_target, target)
            self._condition.notify_all()
            while self._processed < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._state in {"closed", "failed"}:
                    return False
                self._condition.wait(remaining)
            return self._state != "failed"

    def close(self, timeout: float = 5.0) -> bool:
        """停止收取、排空、释放目标锁。超时返回 False，不能误报已关闭。"""
        with self._condition:
            if self._state == "new":
                self._state = "closed"
                return True
            if self._state == "running":
                self._state = "closing"
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._queue and self._state == "running":
                        self._condition.wait()
                    if not self._queue:
                        break
                    deadline = time.monotonic() + self.config.flush_interval
                    while (
                        len(self._queue) < self.config.batch_size
                        and self._state == "running"
                        and self._flush_target <= self._processed
                    ):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._condition.wait(remaining)
                    batch = [
                        self._queue.popleft()
                        for _ in range(min(len(self._queue), self.config.batch_size))
                    ]
                    for entry, _ in batch:
                        self._queued_levels[entry.level] -= 1
                    self._inflight = len(batch)
                self._process_batch(batch)
                with self._condition:
                    self._processed = batch[-1][0].sequence
                    self._inflight = 0
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                self._state = "failed"
                self._last_file_error = f"Writer stopped: {type(exc).__name__}"
        finally:
            try:
                self._sink.close()
            except OSError as exc:
                with self._condition:
                    self._last_file_error = f"Close failed: {type(exc).__name__}"
                    self._state = "failed"
            with self._condition:
                if self._state != "failed":
                    self._state = "closed"
                self._condition.notify_all()

    def _process_batch(self, batch: list[tuple[LogEntry, bytes]]) -> None:
        now = time.monotonic()
        rate = self.config.disk_bytes_per_second if self.config.file_enabled else 0
        self._tokens = min(self._bucket_capacity, self._tokens + (now - self._token_time) * rate)
        self._token_time = now
        chosen: set[int] = set()
        dropped: Counter[str] = Counter()
        for index in sorted(range(len(batch)), key=lambda i: -batch[i][0].level):
            entry, payload = batch[index]
            if rate == 0 or len(payload) <= self._tokens:
                chosen.add(index)
                if rate:
                    self._tokens -= len(payload)
            else:
                dropped[entry.level.name] += 1
        selected = [item for index, item in enumerate(batch) if index in chosen]
        before_written = self._sink.records_written
        before_bytes = self._sink.bytes_written
        before_calls = self._sink.write_calls
        file_error = self._last_file_error
        if self.config.file_enabled and selected and now >= self._retry_at:
            try:
                self._sink.write_batch([payload for _, payload in selected])
                file_error = None
            except Exception as exc:
                file_error = f"{type(exc).__name__}: {safe_text(str(exc), 256)}"
                self._retry_at = now + self.config.retry_interval
                self._sink.close_stream()
        saved = self._sink.records_written - before_written
        if self.config.file_enabled:
            for entry, _ in selected[saved:]:
                dropped[entry.level.name] += 1
        console_error: str | None = None
        if self.config.console and self._console is not None:
            try:
                self._console.write(
                    "".join(
                        f"{entry.timestamp} {entry.level.name:<8} {entry.logger}: {entry.message}\n"
                        + (entry.exception + "\n" if entry.exception else "")
                        for entry, _ in batch
                    )
                )
                self._console.flush()
            except Exception as exc:
                console_error = f"{type(exc).__name__}: {safe_text(str(exc), 256)}"
        with self._condition:
            self._counts["written"] += saved
            self._counts["bytes_written"] += self._sink.bytes_written - before_bytes
            self._counts["write_batches"] += self._sink.write_calls - before_calls
            self._counts["rate_limited"] += len(batch) - len(selected)
            if self.config.file_enabled:
                self._counts["file_dropped"] += len(selected) - saved
            else:
                self._counts["file_skipped"] += len(batch)
            self._dropped.update(dropped)
            self._last_file_error = file_error
            self._last_console_error = console_error
            if console_error:
                self._counts["console_errors"] += 1
