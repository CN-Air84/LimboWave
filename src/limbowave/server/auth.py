"""SQLite-backed device authorization for the headless server."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import open_connection
from limbowave.web.auth import AuthError, AuthStore, Device, digest

_CSRF_LABEL = b"limbowave-server-csrf-v1"


class PersistentAuthStore(AuthStore):
    """Persist only digests and metadata; pairing windows stay process-local."""

    def __init__(
        self,
        database: Path,
        *,
        clock: Callable[[], float] = time.time,
        idle_ttl: float = 7200,
        absolute_ttl: float = 30 * 24 * 3600,
        max_devices: int = 8,
    ) -> None:
        super().__init__(
            clock=clock,
            idle_ttl=idle_ttl,
            absolute_ttl=absolute_ttl,
            max_devices=max_devices,
        )
        self._database = database
        with closing(open_connection(database)) as conn, conn:
            migrate(conn)
        self._reload()

    def _reload(self) -> None:
        self.devices.clear()
        now = self.clock()
        with closing(open_connection(self._database)) as conn:
            rows = conn.execute(
                "SELECT id, name, token_hash, csrf_hash, created_at, touched_at "
                "FROM server_devices WHERE revoked_at IS NULL AND absolute_expires_at > ?",
                (now,),
            ).fetchall()
        for row in rows:
            device = Device(row[0], row[1], row[2], row[3], float(row[4]), float(row[5]))
            if now - device.touched < self.idle_ttl:
                self.devices[device.id] = device

    def csrf_token(self, token: str) -> str:
        return hmac.new(token.encode("utf-8"), _CSRF_LABEL, hashlib.sha256).hexdigest()

    def issue(self, name: str) -> dict[str, object]:
        self._prune_expired()
        if len(self.list_devices()) >= self.max_devices:
            raise AuthError()
        token = secrets.token_urlsafe(32)
        csrf = self.csrf_token(token)
        now = self.clock()
        device = Device(
            secrets.token_urlsafe(16),
            name[:80],
            digest(token),
            digest(csrf),
            now,
            now,
        )
        with closing(open_connection(self._database)) as conn, conn:
            conn.execute(
                "INSERT INTO server_devices "
                "(id, name, token_hash, csrf_hash, created_at, touched_at, "
                "absolute_expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    device.id,
                    device.name,
                    device.token_hash,
                    device.csrf_hash,
                    now,
                    now,
                    now + self.absolute_ttl,
                ),
            )
        self.devices[device.id] = device
        return {
            "status": "paired",
            "token": token,
            "csrf_token": csrf,
            "device_id": device.id,
        }

    def _prune_expired(self) -> None:
        now = self.clock()
        with closing(open_connection(self._database)) as conn, conn:
            conn.execute(
                "UPDATE server_devices SET revoked_at = ? WHERE revoked_at IS NULL "
                "AND (absolute_expires_at <= ? OR touched_at <= ?)",
                (now, now, now - self.idle_ttl),
            )
        for device_id, device in list(self.devices.items()):
            if now - device.created >= self.absolute_ttl or now - device.touched >= self.idle_ttl:
                device.revoked.set()
                self.devices.pop(device_id, None)

    def _load_token(self, token_hash: str) -> Device | None:
        now = self.clock()
        with closing(open_connection(self._database)) as conn:
            row = conn.execute(
                "SELECT id, name, token_hash, csrf_hash, created_at, touched_at, "
                "absolute_expires_at FROM server_devices "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        if row is None or float(row[6]) <= now or now - float(row[5]) >= self.idle_ttl:
            return None
        return Device(row[0], row[1], row[2], row[3], float(row[4]), float(row[5]))

    def valid(self, device: Device) -> bool:
        if device.revoked.is_set():
            return False
        now = self.clock()
        if now - device.created >= self.absolute_ttl or now - device.touched >= self.idle_ttl:
            self.revoke(device.id)
            return False
        with closing(open_connection(self._database)) as conn:
            row = conn.execute(
                "SELECT revoked_at FROM server_devices WHERE id = ?", (device.id,)
            ).fetchone()
        if row is None or row[0] is not None:
            device.revoked.set()
            self.devices.pop(device.id, None)
            return False
        return True

    def authenticate(self, token: str, *, touch: bool = True) -> Device:
        token_hash = digest(token)
        device = next(
            (
                item
                for item in self.devices.values()
                if hmac.compare_digest(item.token_hash, token_hash)
            ),
            None,
        )
        if device is None or device.revoked.is_set():
            device = self._load_token(token_hash)
            if device is not None:
                self.devices[device.id] = device
        if device is None or not self.valid(device):
            raise AuthError()
        if touch:
            device.touched = self.clock()
            with closing(open_connection(self._database)) as conn, conn:
                conn.execute(
                    "UPDATE server_devices SET touched_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (device.touched, device.id),
                )
        return device

    def revoke(self, device_id: str) -> None:
        now = self.clock()
        with closing(open_connection(self._database)) as conn, conn:
            changed = conn.execute(
                "UPDATE server_devices SET revoked_at = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (now, device_id),
            ).rowcount
        device = self.devices.pop(device_id, None)
        if device is not None:
            device.revoked.set()
        if changed and self.on_revoke is not None:
            self.on_revoke(device_id)

    def invalidate(self) -> None:
        # Listener/runtime invalidation closes active sessions but does not silently
        # revoke durable device authorization. A fresh server instance reloads it.
        for device in self.devices.values():
            device.revoked.set()
        self.devices.clear()
        self.pending.clear()
        self.ticket_hash = self.code_hash = ""
        self.window_expires = 0

    def list_devices(self) -> list[dict[str, str]]:
        self._prune_expired()
        with closing(open_connection(self._database)) as conn:
            rows = conn.execute(
                "SELECT id, name FROM server_devices WHERE revoked_at IS NULL "
                "ORDER BY created_at, id"
            ).fetchall()
        return [{"device_id": row[0], "name": row[1]} for row in rows]
