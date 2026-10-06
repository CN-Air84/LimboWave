"""Qt-free adapter over the existing runtime (not a second kernel).

All entry points run on the owning asyncio loop. DTOs contain only public fields.
History I/O uses HistoryReader and the bounded HistoryService page methods.
History cursors are opaque to this adapter and validated by the history service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from limbowave.application.events import ChatEvent
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.command_receipts import CommandReceipts, RuntimeConflict
from limbowave.application.services.event_broker import EventBroker
from limbowave.application.services.history_reader import HistoryReader
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import CommandBusy
from limbowave.application.services.session_controller import SessionController
from limbowave.domain.conversation import Branch, Conversation

__all__ = ["RuntimeConflict", "RuntimeFacade"]


@runtime_checkable
class HistoryPages(Protocol):
    """Native bounded storage pagination; no whole-history fallback is permitted."""

    def page_conversations(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]: ...

    def page_messages(
        self,
        conversation_id: str,
        branch_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...


class RuntimeFacade:
    def __init__(
        self,
        session: SessionController,
        uow_factory: UnitOfWorkFactory,
        *,
        models_provider: Callable[[], list[dict[str, Any]]] | None = None,
        select_model: Callable[[str], Awaitable[None]] | None = None,
        ready: Callable[[], bool] | None = None,
        broker: EventBroker | None = None,
        receipts: CommandReceipts | None = None,
    ) -> None:
        self.session = session
        self._coordinator = session.coordinator()
        self._uow_factory = uow_factory
        self.events = broker or EventBroker()
        self.epoch = self.events.epoch
        self._receipts = receipts or CommandReceipts(self.epoch)
        if self._receipts.epoch != self.epoch:
            raise ValueError("receipt/broker epoch mismatch")
        self._models_provider = models_provider or (lambda: [])
        self._select_model = select_model
        self._ready = ready or (lambda: True)
        self._reader = HistoryReader()
        history = HistoryService(uow_factory)
        self._history: HistoryPages | None = history if isinstance(history, HistoryPages) else None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._valid = True
        self._unsubscribe = session.subscribe(self._on_event)

    @property
    def revision(self) -> int:
        return self._coordinator.revision

    async def state(self) -> dict[str, Any]:
        # No await between snapshot and cursor: replay bridges state -> subscribe.
        return {
            "server_epoch": self.epoch,
            "revision": self.revision,
            "available": self._valid
            and not self._closed
            and self.session.available
            and self._ready(),
            "busy": self.session.busy,
            "conversation_id": self.session.conversation_id,
            "branch_id": self.session.branch_id,
            "run_id": self.session.run_id,
            "seq": self.events.seq,
            "stream": (
                self._coordinator.stream_snapshot()
                if self._valid and not self._closed and self._ready()
                else None
            ),
        }

    async def models(self) -> list[dict[str, Any]]:
        self._require_available()
        return [
            {"id": str(row["id"]), "name": str(row.get("name", row["id"]))}
            for row in self._models_provider()
        ]

    def _require_available(self) -> None:
        if self._closed or not self._valid or not self.session.available or not self._ready():
            raise RuntimeConflict("runtime_unavailable", 503)

    def receipt(self, device: str, command_id: str) -> dict[str, Any] | None:
        self._require_available()
        return self._receipts.receipt(device, command_id)

    async def execute(self, device_id: str, command: dict[str, Any]) -> dict[str, Any]:
        self._require_available()
        command = deepcopy(command)
        record, fresh = self._receipts.register(device_id, command)
        if not fresh:
            return record
        task = asyncio.create_task(self._execute_registered(device_id, command, self._receipts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _execute_registered(
        self, device: str, command: dict[str, Any], receipts: CommandReceipts
    ) -> dict[str, Any]:
        status, error, run_id = "accepted", None, None
        target: tuple[str | None, str | None] = (None, None)
        try:
            # Import only at execution; no silent fallback to desktop privileges.
            from limbowave.application.services.run_origin import remote_origin

            self._validate_command(command)
            if command["server_epoch"] != self.epoch:
                raise RuntimeConflict("epoch_mismatch")
            self._require_available()
            with remote_origin():
                if command["type"] == "abort":
                    if command["run_id"] != self._coordinator.active_run_id:
                        raise RuntimeConflict("run_mismatch")
                    run_id = self.session.run_id
                    target = self.session.conversation_id, self.session.branch_id
                    await self.session.abort()
                else:
                    with self._coordinator.command_scope():
                        if self.session.busy:
                            raise RuntimeConflict("runtime_busy")
                        if command["expected_revision"] != self.revision:
                            raise RuntimeConflict("revision_mismatch")
                        kind = command["type"]
                        if kind == "new_session":
                            target = await self._reader.read(self._create_draft)
                        elif kind == "select_model":
                            await self._set_model(command["model_id"])
                        elif kind == "send":
                            send_target: tuple[str, str] = (
                                command["conversation_id"],
                                command["branch_id"],
                            )
                            target = send_target
                            await self._reader.read(self._validate_target, *send_target)
                            if send_target != (
                                self.session.conversation_id,
                                self.session.branch_id,
                            ):
                                self._coordinator.notify_remote_target_changing(*send_target)
                                if not await self.session.switch_conversation(*send_target):
                                    raise RuntimeConflict("target_restore_failed")
                            if command.get("model_id") is not None:
                                await self._set_model(command["model_id"])
                            run_id = await self.session.send(command["text"])
                            if run_id is None:
                                raise RuntimeConflict("send_rejected")
                        if kind != "new_session":
                            target = self.session.conversation_id, self.session.branch_id
        except (RuntimeConflict, CommandBusy) as exc:
            status = "rejected"
            error = {
                "code": getattr(exc, "code", "runtime_busy"),
                "status": getattr(exc, "status", 409),
            }
        except Exception:
            # Never transport exception text, credentials or local paths.
            status, error = "failed", {"code": "command_failed", "status": 500}
        result = receipts.finish(
            device,
            command["client_command_id"],
            status=status,
            revision=self.revision,
            run_id=run_id,
            conversation_id=target[0],
            branch_id=target[1],
            error=error,
        )
        return result or {
            "server_epoch": receipts.epoch,
            "client_command_id": command["client_command_id"],
            "status": "rejected",
            "error": {"code": "device_revoked", "status": 401},
        }

    @staticmethod
    def _validate_command(command: dict[str, Any]) -> None:
        common = {"type", "server_epoch", "client_command_id", "expected_revision"}
        fields = {
            "new_session": set(),
            "send": {"conversation_id", "branch_id", "text", "model_id"},
            "abort": {"run_id"},
            "select_model": {"model_id"},
        }
        kind = command.get("type")
        if not isinstance(kind, str) or kind not in fields or set(command) - common - fields[kind]:
            raise RuntimeConflict("invalid_command", 400)
        if type(command.get("expected_revision")) is not int or command["expected_revision"] < 0:
            raise RuntimeConflict("invalid_revision", 400)
        required = fields[kind] - ({"model_id"} if kind == "send" else set())
        if any(
            not isinstance(command.get(key), str) or not command[key].strip() for key in required
        ):
            raise RuntimeConflict("invalid_command", 400)
        if "model_id" in command and not isinstance(command["model_id"], str):
            raise RuntimeConflict("invalid_model", 400)

    async def _set_model(self, model_id: str) -> None:
        if self._select_model is None or model_id not in {row["id"] for row in await self.models()}:
            raise RuntimeConflict("model_unavailable", 400)
        try:
            await self._select_model(model_id)
        finally:
            # A provider may accept the physical switch before a later settings
            # call fails. Invalidate optimistic revisions even on partial failure.
            self._coordinator.bump_revision()

    def _create_draft(self) -> tuple[str, str]:
        now = datetime.now(UTC)
        conversation_id, branch_id = "conv_" + uuid4().hex, "branch_" + uuid4().hex
        with self._uow_factory() as uow:
            uow.conversations.add(Conversation(conversation_id, "新会话", now))
            uow.branches.add(Branch(branch_id, conversation_id, now))
            uow.commit()
        return conversation_id, branch_id

    def _validate_target(self, conversation_id: str, branch_id: str) -> None:
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if (
                uow.conversations.get(conversation_id) is None
                or branch is None
                or branch.conversation_id != conversation_id
            ):
                raise RuntimeConflict("target_not_found", 404)

    def _require_history(self, limit: int) -> HistoryPages:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RuntimeConflict("invalid_limit", 400)
        if self._history is None:
            raise RuntimeConflict("history_unavailable", 503)
        return self._history

    async def conversations(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        self._require_available()
        history = self._require_history(limit)
        result = await self._reader.read(history.page_conversations, limit=limit, cursor=cursor)
        self._require_available()
        return result

    async def messages(
        self, conversation_id: str, branch_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        self._require_available()
        history = self._require_history(limit)
        result = await self._reader.read(
            history.page_messages, conversation_id, branch_id, limit=limit, cursor=cursor
        )
        self._require_available()
        return result

    def _on_event(self, event: ChatEvent) -> None:
        if self._closed or not self._valid:
            return
        kind, data = event.kind, event.data
        allowed = {
            "user": ("text",),
            "assistant_start": (),
            "assistant_delta": ("text", "delta"),
            "thinking_delta": ("text", "delta"),
            "assistant_end": ("text", "thinking", "stop_reason"),
            "tool": ("name", "phase", "is_error", "tool_call_id"),
            "settled": (),
            "run_failed": (),
            "run_interrupted": (),
            "error": (),
            "branched": (),
            "retrying": ("attempt", "max_attempts", "delay_ms"),
            "first_response_completed": (),
        }
        if kind not in allowed:
            return
        payload = {
            key: data[key]
            for key in allowed[kind]
            if key in data and isinstance(data[key], (str, int, float, bool, type(None)))
        }
        scope = self._coordinator.event_scope()
        scope["message_id"] = data.get("message_id") or scope["message_id"]
        self.events.publish(kind, payload, **scope)

    def invalidate(self) -> None:
        """Call on vault lock/reset. Caller must revoke authentication as well."""
        if self._valid and not self._closed:
            self._valid = False
            self.events.invalidate()

    def rotate_epoch(self) -> str:
        """Call before a new Web listener accepts traffic, after auth revocation.

        Existing run/task ownership is unchanged. Old tasks retain their old
        receipt store and cannot populate the new authenticated epoch.
        Vault invalidation is permanent: rotate does not unlock a vault.
        """
        if self._closed or not self._valid:
            raise RuntimeConflict("runtime_unavailable", 503)
        self.epoch = self.events.rotate_epoch()
        self._receipts = CommandReceipts(self.epoch, capacity=self._receipts.capacity)
        return self.epoch

    def revoke_device(self, device_id: str) -> None:
        """Only after authentication revocation; receipt retention ends here."""
        self._receipts.revoke(device_id)

    async def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        self.events.close()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        await self._reader.close()
