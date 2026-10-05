"""Content-only fallback for Pi's invalidated post-compaction usage.

Mirror Pi's estimateTokens heuristic: ceil(UTF-16 chars / 4) per message,
with 1200 tokens per image. Count only effective get_messages content, never
assistant usage (which may describe the discarded pre-compaction prompt),
image base64, signatures, or other session/audit metadata.
"""

from __future__ import annotations

import json
from typing import Any


def _text_chars(value: object) -> int:
    # JavaScript String.length counts UTF-16 code units, including emoji pairs.
    if not isinstance(value, str):
        return 0
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _content_chars(content: object) -> int:
    if isinstance(content, str):
        return _text_chars(content)
    if not isinstance(content, list):
        return 0
    return sum(
        4800 if block.get("type") == "image" else _text_chars(block.get("text"))
        for block in content
        if isinstance(block, dict) and block.get("type") in {"text", "image"}
    )


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate one effective Pi message without consulting stale billing usage."""
    role = message.get("role")
    chars = 0
    if role in {"user", "custom", "toolResult"}:
        chars = _content_chars(message.get("content"))
    elif role == "assistant":
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                chars += _text_chars(block.get("text"))
            elif kind == "thinking":
                chars += _text_chars(block.get("thinking"))
            elif kind == "toolCall":
                arguments = json.dumps(
                    block.get("arguments", {}), ensure_ascii=False, separators=(",", ":")
                )
                chars += _text_chars(block.get("name")) + _text_chars(arguments)
    elif role == "bashExecution":
        chars = _text_chars(message.get("command")) + _text_chars(message.get("output"))
    elif role in {"branchSummary", "compactionSummary"}:
        chars = _text_chars(message.get("summary"))
    return (chars + 3) // 4
