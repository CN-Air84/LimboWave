from __future__ import annotations

from pathlib import Path

from limbowave.server.auth import PersistentAuthStore
from limbowave.web.auth import AuthError


def test_pairing_window_is_not_reopened_by_restart(tmp_path: Path) -> None:
    database = tmp_path / "server.db"
    first = PersistentAuthStore(database)
    ticket = first.open_pairing()["ticket"]

    restarted = PersistentAuthStore(database)
    try:
        restarted.pair(ticket=str(ticket))
    except AuthError:
        pass
    else:  # pragma: no cover - security assertion
        raise AssertionError("pairing window survived restart")


def test_pairing_creates_persistent_authorization(tmp_path: Path) -> None:
    database = tmp_path / "server.db"
    auth = PersistentAuthStore(database)
    window = auth.open_pairing()
    issued = auth.pair(ticket=str(window["ticket"]), name="browser")

    restarted = PersistentAuthStore(database)
    assert restarted.authenticate(str(issued["token"])).name == "browser"
