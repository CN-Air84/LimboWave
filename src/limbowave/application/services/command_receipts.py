"""Epoch-local, bounded command deduplication. Records live until device revocation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


class RuntimeConflict(RuntimeError):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class CommandReceipts:
    """Single-event-loop store. Never evicts a still-valid command."""

    def __init__(self, epoch: str, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.epoch = epoch
        self.capacity = capacity
        self._records: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    def register(self, device: str, command: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if command.get("server_epoch") != self.epoch:
            raise RuntimeConflict("epoch_mismatch")
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
        if len(encoded.encode()) > 256_000:
            raise RuntimeConflict("command_too_large", 413)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        key = device, command_id
        existing = self._records.get(key)
        if existing:
            if existing[0] != fingerprint:
                raise RuntimeConflict("command_id_reused")
            return deepcopy(existing[1]), False
        if len(self._records) >= self.capacity:
            raise RuntimeConflict("receipt_capacity", 503)
        record = {
            "server_epoch": self.epoch,
            "client_command_id": command_id,
            "status": "pending",
            "revision": None,
            "run_id": None,
            "conversation_id": None,
            "branch_id": None,
            "error": None,
        }
        self._records[key] = fingerprint, record
        return deepcopy(record), True

    def receipt(self, device: str, command_id: str) -> dict[str, Any] | None:
        value = self._records.get((device, command_id))
        return deepcopy(value[1]) if value else None

    def finish(self, device: str, command_id: str, **fields: Any) -> dict[str, Any] | None:
        value = self._records.get((device, command_id))
        if value is None:
            return None
        value[1].update(deepcopy(fields))
        return deepcopy(value[1])

    def revoke(self, device: str) -> None:
        for key in list(self._records):
            if key[0] == device:
                del self._records[key]
