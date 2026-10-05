"""Async boundary for process-isolated conversation storage work."""

from typing import Any, Protocol


class StorageWorker(Protocol):
    async def call(
        self, operation: str, location: tuple[str | None, str | None],
        *args: Any, **kwargs: Any,
    ) -> Any: ...
