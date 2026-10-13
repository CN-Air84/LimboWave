from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.server.auth import PersistentAuthStore
from limbowave.web.auth import AuthError


def test_device_survives_store_restart_and_token_is_not_stored(tmp_path: Path) -> None:
    database = tmp_path / "server.db"
    first = PersistentAuthStore(database)
    issued = first.issue("browser")
    token = str(issued["token"])
    device_id = str(issued["device_id"])

    raw = database.read_bytes()
    assert token.encode() not in raw

    restarted = PersistentAuthStore(database)
    device = restarted.authenticate(token)
    assert device.id == device_id
    restarted.csrf(device, str(issued["csrf_token"]))


def test_revocation_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "server.db"
    auth = PersistentAuthStore(database)
    issued = auth.issue("desktop")
    auth.revoke(str(issued["device_id"]))

    restarted = PersistentAuthStore(database)
    with pytest.raises(AuthError):
        restarted.authenticate(str(issued["token"]))
    assert restarted.list_devices() == []


def test_idle_expiry_does_not_revoke_durable_authorization(tmp_path: Path) -> None:
    now = [100.0]
    database = tmp_path / "server.db"
    auth = PersistentAuthStore(database, clock=lambda: now[0], idle_ttl=5, absolute_ttl=10)
    issued = auth.issue("browser")
    now[0] = 106.0
    restarted = PersistentAuthStore(database, clock=lambda: now[0], idle_ttl=5, absolute_ttl=10)
    assert restarted.authenticate(str(issued["token"])).id == issued["device_id"]

    now[0] = 111.0
    expired = PersistentAuthStore(database, clock=lambda: now[0], idle_ttl=5, absolute_ttl=10)
    with pytest.raises(AuthError):
        expired.authenticate(str(issued["token"]))


def test_invalidate_does_not_revoke_durable_authorization(tmp_path: Path) -> None:
    database = tmp_path / "server.db"
    auth = PersistentAuthStore(database)
    issued = auth.issue("browser")
    auth.invalidate()
    # Runtime invalidation closes the listener; durable authorization remains.
    assert auth.list_devices()[0]["device_id"] == issued["device_id"]

    restarted = PersistentAuthStore(database)
    assert restarted.authenticate(str(issued["token"])).id == issued["device_id"]
