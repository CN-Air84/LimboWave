from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.server.command_store import PersistentCommandStore


def test_command_receipt_is_idempotent_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "commands.db"
    first = PersistentCommandStore(db, "epoch")
    command = {"server_epoch": "epoch", "client_command_id": "op", "type": "send", "text": "hello"}
    initial, fresh = first.register("device", command)
    assert fresh and initial["status"] == "pending"
    first.finish("device", "op", status="accepted", run_id="run")

    restarted = PersistentCommandStore(db, "epoch")
    same, fresh = restarted.register("device", command)
    assert not fresh and same["status"] == "accepted" and same["run_id"] == "run"
    with pytest.raises(RuntimeConflict, match="command_id_reused"):
        restarted.register("device", {**command, "text": "different"})


def test_pending_receipt_is_interrupted_on_restart(tmp_path: Path) -> None:
    db = tmp_path / "commands.db"
    first = PersistentCommandStore(db, "epoch")
    first.register("device", {"server_epoch": "epoch", "client_command_id": "op", "type": "send"})
    restarted = PersistentCommandStore(db, "epoch")
    receipt = restarted.receipt("device", "op")
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "runtime_interrupted"
