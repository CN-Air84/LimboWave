"""有界快照与脱敏。未知对象只记类型，绝不遍历其属性或调用 repr。"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from itertools import islice
from typing import Any

from limbowave.domain.redaction import (
    REDACTED,
    SENSITIVE_FIELD_NAMES,
    SENSITIVE_HEADER_NAMES,
    redact_text,
)
from limbowave.infrastructure.diagnostics.models import LogEntry, LogLevel

_SENSITIVE = SENSITIVE_FIELD_NAMES | SENSITIVE_HEADER_NAMES | {"key", "passwd"}
_ASSIGNMENT = re.compile(
    r"""(?ix)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"""
    r"""password|passwd|token|secret|credential|key)["']?\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|[^\s&,;}]+)"""
)
_AUTH = re.compile(
    r"""(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*)[^\r\n]+"""
)
_USERINFO = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "context"}


def safe_text(text: str, limit: int = 4096) -> str:
    # 先限制扫描量，再脱敏，最后按展示长度截断；避免超长异常制造 CPU 尖峰。
    prefix = text[: limit * 2]
    prefix = _AUTH.sub(lambda m: m[1] + REDACTED, prefix)
    prefix = _ASSIGNMENT.sub(lambda m: m[1] + REDACTED, prefix)
    prefix = _USERINFO.sub(lambda m: m[1] + REDACTED + "@", prefix)
    prefix = redact_text(prefix)
    return prefix[:limit] + ("…[truncated]" if len(text) > limit or len(prefix) > limit else "")


def safe_context(value: Mapping[str, Any]) -> str:
    budget = 128
    seen: set[int] = set()

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 6:
            return "[truncated]"
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            return item if item.bit_length() < 1024 else "[large integer]"
        if type(item) is float:
            return item if math.isfinite(item) else "[non-finite]"
        if isinstance(item, str):
            return safe_text(item)
        if isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                return "[cycle]"
            seen.add(id(item))
            try:
                if isinstance(item, dict):
                    result: dict[str, Any] = {}
                    for key, child in islice(item.items(), 64):
                        if not isinstance(key, str):
                            continue
                        result[safe_text(key, 128)] = (
                            REDACTED if key.lower() in _SENSITIVE else visit(child, depth + 1)
                        )
                        if budget < 0:
                            break
                    if len(item) > len(result):
                        result["_truncated"] = True
                    return result
                children = [
                    visit(child, depth + 1) for child in islice(item, min(64, max(0, budget)))
                ]
                if len(item) > len(children):
                    children.append("[truncated]")
                return children
            finally:
                seen.remove(id(item))
        return f"<{safe_text(type(item).__name__, 128)}>"

    snapshot = dict(islice(value.items(), 64))
    return json.dumps(visit(snapshot), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _exception_text(error: BaseException) -> str:
    """不读取源码/局部变量，也不把 traceback 对象留在队列里。"""
    parts: list[str] = []
    seen: set[int] = set()
    for _ in range(5):
        if id(error) in seen:
            break
        seen.add(id(error))
        parts.append(f"{type(error).__name__}: {safe_text(str(error))}")
        tb = error.__traceback__
        for _ in range(32):
            if tb is None:
                break
            code = tb.tb_frame.f_code
            parts.append(
                f'  File "{safe_text(code.co_filename, 256)}", '
                f"line {tb.tb_lineno}, in {safe_text(code.co_name, 128)}"
            )
            tb = tb.tb_next
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is None:
            break
        parts.append("Caused by / during handling:")
        error = cause
    return safe_text("\n".join(parts), 8192)


def snapshot_record(
    record: logging.LogRecord, context: Mapping[str, Any], session_id: str
) -> LogEntry:
    fields = dict(islice(context.items(), 64))
    embedded = getattr(record, "context", None)
    if isinstance(embedded, dict):
        fields.update(islice(embedded.items(), 64))
    fields.update(islice(((k, v) for k, v in vars(record).items() if k not in _STANDARD), 64))
    exception = ""
    exception_type = ""
    if record.exc_info and record.exc_info[1] is not None:
        exception_type = safe_text(type(record.exc_info[1]).__name__, 128)
        exception = _exception_text(record.exc_info[1])
    elif record.exc_text:
        exception = safe_text(record.exc_text, 8192)
    if record.stack_info:
        exception = safe_text(exception + "\n" + record.stack_info, 8192)
    message = safe_text(record.getMessage())
    context_json = safe_context(fields)

    def bounded_int(value: Any) -> int:
        return value if type(value) is int and 0 <= value < 2**63 else 0

    return LogEntry(
        sequence=0,
        timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        level=LogLevel.from_logging(record.levelno),
        logger=safe_text(record.name, 128),
        message=message,
        pathname=safe_text(record.pathname, 256),
        lineno=bounded_int(record.lineno),
        function=safe_text(record.funcName or "", 128),
        process=bounded_int(record.process),
        thread=safe_text(record.threadName or "", 128),
        exception=exception,
        exception_type=exception_type,
        context_json=context_json,
        truncated=(
            "…[truncated]" in message + exception + context_json
            or '"[truncated]"' in context_json
            or '"_truncated":true' in context_json
        ),
        session_id=session_id,
    )


def encode_entry(entry: LogEntry, max_bytes: int) -> tuple[LogEntry, bytes]:
    def encode() -> bytes:
        return (
            json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8", errors="backslashreplace")

    payload = encode()
    if len(payload) <= max_bytes:
        return entry, payload
    entry = replace(entry, context_json='{"_truncated":true}', truncated=True)
    payload = encode()
    while len(payload) > max_bytes:
        entry = replace(
            entry,
            message=entry.message[: len(entry.message) // 2],
            exception=entry.exception[: len(entry.exception) // 2],
            exception_type=entry.exception_type[: len(entry.exception_type) // 2],
            pathname=entry.pathname[: len(entry.pathname) // 2],
            logger=entry.logger[: len(entry.logger) // 2],
            function=entry.function[: len(entry.function) // 2],
            thread=entry.thread[: len(entry.thread) // 2],
        )
        payload = encode()
    return entry, payload
