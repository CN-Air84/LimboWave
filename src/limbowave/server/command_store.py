"""Durable operation-id receipts for safe retry and restart recovery."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from limbowave.application.services.command_receipts import CommandReceipts, RuntimeConflict
from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import open_connection


class PersistentCommandStore(CommandReceipts):
    def __init__(
        self,
        database: Path,
        epoch: str,
        *,
        capacity: int = 4096,
        recover_pending: bool = True,
    ) -> None:
        super().__init__(epoch, capacity=capacity)
        self._database = database
        with closing(open_connection(database)) as conn, conn:
            migrate(conn)
            if recover_pending:
                now = time.time()
                conn.execute(
                    "UPDATE server_commands SET status = 'failed', "
                    "error_code = 'runtime_interrupted', error_status = 409, updated_at = ? "
                    "WHERE status = 'pending'",
                    (now,),
                )

    @staticmethod
    def _fingerprint(device: str, command: dict[str, Any]) -> tuple[str, str]:
        command_id = command.get("client_command_id")
        if (
            not isinstance(device, str)
            or not device
            or not isinstance(command_id, str)
            or not command_id
            or len(command_id) > 200
        ):
            raise RuntimeConflict("invalid_command_id", 400)
        try:
            encoded = json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise RuntimeConflict("invalid_command", 400) from None
        if len(encoded.encode("utf-8")) > 256_000:
            raise RuntimeConflict("command_too_large", 413)
        return command_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _record(row: tuple[Any, ...]) -> dict[str, Any]:
        error = None
        if row[8] is not None:
            error = {"code": row[8], "status": row[9] or 409}
        return {
            "server_epoch": row[0],
            "client_command_id": row[1],
            "status": row[2],
            "revision": row[3],
            "run_id": row[4],
            "conversation_id": row[5],
            "branch_id": row[6],
            "error": error,
        }

    def register(self, device: str, command: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if command.get("server_epoch") != self.epoch:
            raise RuntimeConflict("epoch_mismatch")
        command_id, fingerprint = self._fingerprint(device, command)
        with closing(open_connection(self._database)) as conn, conn:
            row = conn.execute(
                "SELECT server_epoch, operation_id, status, revision, run_id, "
                "conversation_id, branch_id, fingerprint, error_code, error_status "
                "FROM server_commands WHERE device_id = ? AND operation_id = ?",
                (device, command_id),
            ).fetchone()
            if row is not None:
                if row[7] != fingerprint:
                    raise RuntimeConflict("command_id_reused")
                return self._record(row), False
            count = conn.execute("SELECT COUNT(*) FROM server_commands").fetchone()
            if count is not None and int(count[0]) >= self.capacity:
                raise RuntimeConflict("receipt_capacity", 503)
            now = time.time()
            conn.execute(
                "INSERT INTO server_commands "
                "(device_id, operation_id, server_epoch, fingerprint, status, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (device, command_id, self.epoch, fingerprint, now, now),
            )
        return {
            "server_epoch": self.epoch,
            "client_command_id": command_id,
            "status": "pending",
            "revision": None,
            "run_id": None,
            "conversation_id": None,
            "branch_id": None,
            "error": None,
        }, True

    def receipt(self, device: str, command_id: str) -> dict[str, Any] | None:
        with closing(open_connection(self._database)) as conn:
            row = conn.execute(
                "SELECT server_epoch, operation_id, status, revision, run_id, "
                "conversation_id, branch_id, fingerprint, error_code, error_status "
                "FROM server_commands WHERE device_id = ? AND operation_id = ?",
                (device, command_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def finish(self, device: str, command_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.receipt(device, command_id)
        if current is None:
            return None
        current.update(fields)
        raw_error = current.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        with closing(open_connection(self._database)) as conn, conn:
            conn.execute(
                "UPDATE server_commands SET status = ?, revision = ?, run_id = ?, "
                "conversation_id = ?, branch_id = ?, error_code = ?, error_status = ?, "
                "updated_at = ? WHERE device_id = ? AND operation_id = ?",
                (
                    current["status"],
                    current.get("revision"),
                    current.get("run_id"),
                    current.get("conversation_id"),
                    current.get("branch_id"),
                    error.get("code"),
                    error.get("status"),
                    time.time(),
                    device,
                    command_id,
                ),
            )
        return current

    def revoke(self, device: str) -> None:
        with closing(open_connection(self._database)) as conn, conn:
            conn.execute("DELETE FROM server_commands WHERE device_id = ?", (device,))

    def rotated(self, epoch: str) -> PersistentCommandStore:
        return PersistentCommandStore(
            self._database,
            epoch,
            capacity=self.capacity,
            recover_pending=False,
        )
