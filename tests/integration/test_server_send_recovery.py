from __future__ import annotations

from pathlib import Path

from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.server.command_store import PersistentCommandStore


def test_new_epoch_does_not_reuse_old_receipt(tmp_path: Path) -> None:
    db = tmp_path / "commands.db"
    old = PersistentCommandStore(db, "old")
    old.register("device", {"server_epoch": "old", "client_command_id": "op", "type": "send"})
    new = PersistentCommandStore(db, "new")
    try:
        new.register("device", {"server_epoch": "old", "client_command_id": "op", "type": "send"})
    except RuntimeConflict as exc:
        assert exc.code == "epoch_mismatch"
    else:
        raise AssertionError("old epoch accepted")
