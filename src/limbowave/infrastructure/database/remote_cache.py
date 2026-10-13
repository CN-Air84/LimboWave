"""Encrypted local cache for a remote LimboWave server."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from limbowave.infrastructure.crypto.vault import VaultKey


@dataclass(frozen=True, slots=True)
class RemoteCacheState:
    cursor: int
    revision: int


class RemoteCache:
    """Store a full snapshot and incremental change records under one cursor."""

    def __init__(self, database: Path, key: VaultKey) -> None:
        self.database = database
        self.key = key
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS remote_cache ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def save_snapshot(self, payload: dict[str, Any]) -> None:
        self._put("snapshot", payload)

    def load_snapshot(self) -> dict[str, Any] | None:
        return self._get("snapshot")

    def save_state(self, cursor: int, revision: int) -> None:
        self._put("state", {"cursor": cursor, "revision": revision})

    def state(self) -> RemoteCacheState:
        value = self._get("state")
        if value is None:
            return RemoteCacheState(0, 0)
        return RemoteCacheState(int(value["cursor"]), int(value["revision"]))

    def save_change(self, item: dict[str, Any]) -> None:
        cursor = int(item["cursor"])
        self._put(f"change:{cursor}", item)

    def changes(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT value FROM remote_cache WHERE key LIKE 'change:%' ORDER BY key"
            ).fetchall()
        return [self._decode(row[0]) for row in rows]

    def clear_changes_through(self, cursor: int) -> int:
        with closing(sqlite3.connect(self.database)) as conn, conn:
            result = conn.execute(
                "DELETE FROM remote_cache WHERE key LIKE 'change:%' "
                "AND CAST(substr(key, 8) AS INTEGER) <= ?",
                (cursor,),
            )
            return result.rowcount

    def _put(self, key: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        sealed = self.key.encrypt(encoded)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "INSERT INTO remote_cache(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, sealed, datetime.now(UTC).isoformat()),
            )

    def _get(self, key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database)) as conn:
            row = conn.execute(
                "SELECT value FROM remote_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self._decode(row[0])

    def _decode(self, value: str) -> dict[str, Any]:
        return dict(json.loads(self.key.decrypt(value)))
