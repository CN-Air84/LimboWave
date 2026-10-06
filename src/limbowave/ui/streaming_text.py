"""GUI-thread text batching: immediate first output, bounded non-debouncing updates."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

STREAM_INTERVAL_MS = 32


class StreamingTextBuffer(QObject):
    """Coalesce a burst without delaying its first fragment or postponing forever.

    The timer is owned by the receiving widget. A message boundary must flush the
    buffer, or discard it when an authoritative final text replaces the draft.
    Nothing is dropped, and no worker ever touches a QTextDocument.
    """

    def __init__(self, parent: QObject, consume: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._consume = consume
        self._pending: list[str] = []
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(STREAM_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def append(self, text: str) -> None:
        if not text:
            return
        self._pending.append(text)
        if not self._paused and not self._timer.isActive():
            self._deliver()
            self._timer.start()

    def _deliver(self) -> None:
        if self._pending:
            text = "".join(self._pending)
            self._pending.clear()
            self._consume(text)

    def _tick(self) -> None:
        if self._pending:
            self._deliver()
            # Keep a cooldown after each paint. The next empty tick stops it;
            # an isolated fragment after an idle period is immediate again.
            self._timer.start()

    def flush(self) -> None:
        self._timer.stop()
        self._deliver()

    def discard(self) -> None:
        self._timer.stop()
        self._pending.clear()

    def pause(self) -> None:
        self._paused = True
        self._timer.stop()

    def resume(self) -> None:
        self._paused = False
        if self._pending:
            self._deliver()
            self._timer.start()
