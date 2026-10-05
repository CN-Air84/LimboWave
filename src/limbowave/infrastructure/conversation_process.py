"""One warm storage process; no Qt, live sockets or SQLite connections cross IPC."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from limbowave.application.services.attachment_service import AttachmentService
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.file_service import FileService
from limbowave.application.services.image_service import ImageService
from limbowave.application.services.memory_service import MemoryService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.sqlite_repositories import SqliteUnitOfWorkFactory


@dataclass(frozen=True)
class ConversationProcessConfig:
    database: Path
    data_root: Path
    # Only sent through the private multiprocessing pipe; never repr/log/argv/disk.
    key: bytes = field(repr=False)


_factory: SqliteUnitOfWorkFactory | None = None
_attachments: AttachmentService | None = None
_memories: MemoryService | None = None
_OPERATIONS = frozenset({
    "_prepare_regeneration", "_prepare_branch", "_persist_branch", "_persist_run_start",
    "_attachment_prompt", "_memory_prompt", "_finalize_storage", "_prepare_switch",
    "_prepare_retry", "_prepare_fork",
})


def _initialize(config: ConversationProcessConfig) -> None:
    global _factory, _attachments, _memories
    key = VaultKey(config.key)
    _factory = SqliteUnitOfWorkFactory(config.database, key)
    blobs = BlobStore(config.data_root / "blobs", key)
    _attachments = AttachmentService(
        FileService(_factory, blob_store=blobs), ImageService(_factory, blobs)
    )
    _memories = MemoryService(
        _factory, ConfigurationService(JsonConfigRepository(config.data_root / "config.json"))
    )


def _execute(
    operation: str, location: tuple[str | None, str | None],
    args: tuple[Any, ...], kwargs: dict[str, Any],
) -> Any:
    if operation == "pid":
        return os.getpid()
    if operation == "history_view":
        from limbowave.application.history_view_payload import load_history_view

        assert _factory is not None
        return load_history_view(_factory, *args, **kwargs)
    if operation == "history_payload":
        from limbowave.application.history_payload import history_payload
        from limbowave.application.services.history_service import HistoryService

        assert _factory is not None
        branch_id = args[0]
        return history_payload(
            HistoryService(_factory).branch_messages(branch_id),
            uow_factory=_factory, branch_id=branch_id,
        )
    if operation == "prepare_tool_event":
        from limbowave.application.tool_step_payload import prepare_tool_event

        return prepare_tool_event(*args, **kwargs)
    if operation == "permissions":
        from limbowave.application.services.permission_service import PermissionService
        from limbowave.domain.permissions import PermissionPreset

        assert _factory is not None
        service = PermissionService(_factory)
        conversation_id, preset, capabilities, workspace_root = args
        service.set_preset(conversation_id, preset)
        if preset is PermissionPreset.CUSTOM:
            saved = {grant.capability for grant in service.list_grants(conversation_id)}
            if saved != capabilities:
                service.replace_custom_grants(
                    conversation_id, capabilities, workspace_root=workspace_root
                )
        return None
    if operation not in _OPERATIONS:
        raise ValueError("Unsupported conversation storage operation")
    assert _factory is not None and _attachments is not None
    context = (
        args[4] if operation == "_persist_run_start" else RunContext("worker", "worker", "worker")
    )
    coordinator = RunCoordinator(None, _factory, context=lambda: context)
    coordinator._active_conversation_id, coordinator._active_branch_id = location
    coordinator.attachment_builder = _attachments.build
    coordinator.memory_service = _memories
    return getattr(coordinator, operation)(*args, **kwargs)


class ConversationProcess:
    def __init__(self, config: ConversationProcessConfig) -> None:
        self._executor = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize, initargs=(config,),
        )
        self._closed = False

    async def warm(self) -> int:
        return int(await self.call("pid", (None, None)))

    async def call(
        self, operation: str, location: tuple[str | None, str | None],
        *args: Any, **kwargs: Any,
    ) -> Any:
        if self._closed:
            raise RuntimeError("Conversation storage process is closed")
        pending = asyncio.get_running_loop().run_in_executor(
            self._executor, partial(_execute, operation, location, args, kwargs)
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Do not close the vault/reset files while a submitted transaction is still running.
            await asyncio.gather(pending, return_exceptions=True)
            raise

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    def close_unstarted(self) -> None:
        """Startup rollback only: no jobs have been submitted before warm()."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
