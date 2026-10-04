"""流式事件磁带：把一次 provider 请求的流式过程记成**可回放的紧凑序列**。

设计计划 §十三.1 要求请求日志包含「流式事件」。这里刻意**不存增量文本**
（最终消息已经存了完整文本，再存一遍等于双倍存储，而且增量本身对排错价值不大），
转而记录**事件序列 + 时间偏移 + 字符数**：

- ``text`` / ``thinking``：这次增量有多少字符——能看出模型是先思考还是先出字、
  中途是否长时间停顿（相邻事件的时间差）、是不是一次性吐完（只有一条大增量）。
- ``tool.start`` / ``tool.end``：工具在流的哪个位置插入。
- ``retry``：重试发生在第几毫秒（§十三.3 的退避时长在日志里可核对）。
- ``end``：finish reason 与最终字符数。

未知事件类型**不丢弃也不整存**：留一条带截断预览的记录。Pi 的事件类型会随版本
增加，遇到没见过的类型就把原文留下（截断+脱敏），比静默忽略更有用。

上限与截断都**如实记录**（``dropped`` / ``truncated``），不假装磁带是完整的。
"""

from __future__ import annotations

from typing import Any

# 单次请求保留的事件条数上限。流式增量理论上可到上万条，
# 这里只保留前 N 条并记录丢弃数量——排错看的是「形状」，不是每一条增量。
MAX_EVENTS = 2000
# 未知事件类型的预览长度
PREVIEW_CHARS = 240


class StreamTape:
    """一次 provider 请求的流式事件序列。可变对象，落库前转成 ``dict``。"""

    __slots__ = ("_count", "_dropped", "_events", "_text_chars", "_thinking_chars")

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._text_chars = 0
        self._thinking_chars = 0
        self._count = 0
        self._dropped = 0

    def add(self, kind: str, *, offset_ms: int, chars: int = 0, **extra: Any) -> None:
        """记一条事件。超出上限只累加计数器，不再留事件。"""
        self._count += 1
        if kind == "text":
            self._text_chars += chars
        elif kind == "thinking":
            self._thinking_chars += chars
        if len(self._events) >= MAX_EVENTS:
            self._dropped += 1
            return
        event: dict[str, Any] = {"e": kind, "t": max(0, int(offset_ms))}
        if chars:
            event["n"] = int(chars)
        for key, value in extra.items():
            if value is not None and value != "":
                event[key] = value
        self._events.append(event)

    def add_unknown(self, kind: str, *, offset_ms: int, preview: Any) -> None:
        """未知事件类型：留一条带截断预览的记录，不整存。"""
        text = preview if isinstance(preview, str) else repr(preview)
        self.add(kind, offset_ms=offset_ms, preview=text[:PREVIEW_CHARS])

    @property
    def is_empty(self) -> bool:
        return self._count == 0

    def to_dict(self) -> dict[str, Any]:
        """落库形态。计数与丢弃量一起存，读的人能看到磁带是否被截断。"""
        return {
            "events": list(self._events),
            "text_chars": self._text_chars,
            "thinking_chars": self._thinking_chars,
            "event_count": self._count,
            "dropped": self._dropped,
        }


def summarize(tape: dict[str, Any] | None) -> str:
    """一句话摘要（界面折叠态显示用）。"""
    if not tape:
        return "无流式记录"
    count = int(tape.get("event_count") or 0)
    if not count:
        return "无流式记录"
    parts = [
        f"{count} 个事件",
        f"正文 {int(tape.get('text_chars') or 0)} 字",
    ]
    thinking = int(tape.get("thinking_chars") or 0)
    if thinking:
        parts.append(f"思考 {thinking} 字")
    dropped = int(tape.get("dropped") or 0)
    if dropped:
        parts.append(f"截断丢弃 {dropped}")
    return "，".join(parts)
