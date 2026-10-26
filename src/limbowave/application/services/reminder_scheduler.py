"""Cancellable in-app scheduler. Disk work never blocks the GUI event loop."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from limbowave.application.background import run_blocking
from limbowave.application.services.reminder_service import ReminderService
from limbowave.domain.conversation import Message

_LOG = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(
        self, service: ReminderService, on_delivery: Callable[[Message], None], *,
        enabled: Callable[[], bool] = lambda: True, interval: float = 1.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._service = service
        self._on_delivery = on_delivery
        self._enabled = enabled
        self._interval = interval

    async def tick(self) -> None:
        if not self._enabled():
            return
        messages = await run_blocking(self._service.deliver_due)
        for message in messages:
            # Reset/restore can start while a database worker is in flight.
            # Messages are durable even when the current view no longer accepts events.
            if not self._enabled():
                break
            try:
                self._on_delivery(message)
            except Exception as exc:
                _LOG.warning(
                    "reminder.notification_failed", extra={"error_type": type(exc).__name__},
                )

    async def run(self) -> None:
        """An immediate scan catches downtime; each later scan uses the wall clock."""
        failures = 0
        while True:
            try:
                await self.tick()
                failures = 0
            except Exception as exc:
                failures += 1
                # No reminder text, SQL values or decrypted data in diagnostic logs.
                if failures == 1:
                    _LOG.warning("reminder.scan_failed", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(min(30.0, self._interval * 2 ** min(failures, 5)))
