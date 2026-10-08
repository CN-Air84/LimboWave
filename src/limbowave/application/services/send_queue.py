"""Session-only text-send FIFO. Runtime dispatch and persistence remain in the coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal
from uuid import uuid4

QueueState = Literal["waiting", "sending", "failed"]


@dataclass(frozen=True)
class QueuedSend:
    id: str
    scope: tuple[str, str]
    text: str
    state: QueueState = "waiting"
    error: str = ""


class SendQueue:
    MAX_ITEMS = 50

    def __init__(self) -> None:
        self._items: list[QueuedSend] = []
        self.paused_reason = ""

    @property
    def items(self) -> tuple[QueuedSend, ...]:
        return tuple(self._items)

    @property
    def ready(self) -> bool:
        return bool(self._items and not self.paused_reason and self._items[0].state == "waiting")

    def enqueue(self, scope: tuple[str, str], text: str) -> QueuedSend:
        if not all(scope) or not text.strip():
            raise ValueError("请选择会话并输入文字")
        if len(self._items) >= self.MAX_ITEMS:
            raise ValueError("发送队列已满，请等待或取消部分消息")
        item = QueuedSend(uuid4().hex, scope, text.strip())
        self._items.append(item)
        return item

    def begin(self) -> QueuedSend | None:
        if not self.ready:
            return None
        self._items[0] = replace(self._items[0], state="sending")
        return self._items[0]

    def accept(self, item_id: str) -> bool:
        if self._items and self._items[0].id == item_id and self._items[0].state == "sending":
            self._items.pop(0)
            return True
        return False

    def fail(self, item_id: str, error: str) -> None:
        if self._items and self._items[0].id == item_id:
            self._items[0] = replace(self._items[0], state="failed", error=error)
            self.pause(error)

    def cancel(self, item_id: str) -> QueuedSend | None:
        for index, item in enumerate(self._items):
            if item.id == item_id and item.state != "sending":
                self._items.pop(index)
                if not self._items:
                    self.paused_reason = ""
                return item
        return None

    def pause(self, reason: str) -> None:
        if self._items:
            self.paused_reason = reason

    def resume(self) -> None:
        self.paused_reason = ""
        if self._items and self._items[0].state == "failed":
            self._items[0] = replace(self._items[0], state="waiting", error="")

    def clear(self) -> None:
        self._items.clear()
        self.paused_reason = ""
