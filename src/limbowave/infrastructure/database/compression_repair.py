"""One-time repair of legacy forks, using only plaintext structural metadata.

Summaries stay encrypted and are copied verbatim. Existing local version records
(including rejected/failed previews and inactive accepted versions) take precedence:
we cannot infer a historical user decision more precisely from the old schema.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any
from uuid import uuid4

from limbowave.domain.compression_inheritance import compression_survives_fork
from limbowave.domain.conversation import Branch


def repair_missing_branch_compressions(conn: sqlite3.Connection) -> None:
    branches = {
        row[0]: Branch(
            id=row[0],
            conversation_id=row[1],
            created_at=datetime.fromisoformat(row[2]),
            parent_branch_id=row[3],
            forked_from_message_id=row[4],
            include_fork_message=bool(row[5]),
        )
        for row in conn.execute(
            "SELECT id, conversation_id, created_at, parent_branch_id, "
            "forked_from_message_id, include_fork_message FROM branches"
        )
    }
    own: dict[str, list[str]] = {}
    for message_id, branch_id in conn.execute(
        "SELECT id, branch_id FROM messages ORDER BY created_at, id"
    ):
        own.setdefault(branch_id, []).append(message_id)
    cursor = conn.execute("SELECT * FROM compression_versions")
    columns = [column[0] for column in cursor.description]
    versions: dict[str, list[dict[str, Any]]] = {}
    for row in cursor:
        version = dict(zip(columns, row, strict=True))
        versions.setdefault(version["branch_id"], []).append(version)
    paths: dict[str, list[str] | None] = {}

    def visit(branch_id: str, ancestors: frozenset[str]) -> list[str] | None:
        if branch_id in paths:
            return paths[branch_id]
        if branch_id in ancestors or len(ancestors) >= 256 or branch_id not in branches:
            return None
        branch = branches[branch_id]
        prefix: list[str] = []
        if branch.parent_branch_id is not None:
            parent = branches.get(branch.parent_branch_id)
            if parent is None or parent.conversation_id != branch.conversation_id:
                paths[branch_id] = None
                return None
            parent_path = visit(parent.id, ancestors | {branch_id})
            if parent_path is None:
                paths[branch_id] = None
                return None
            prefix = parent_path[:]
            anchor = branch.forked_from_message_id
            if anchor is not None:
                if anchor not in prefix:
                    paths[branch_id] = None
                    return None
                prefix = prefix[: prefix.index(anchor) + int(branch.include_fork_message)]
            if branch_id not in versions:
                inherited = []
                for version in versions.get(parent.id, []):
                    if version["status"] != "accepted" or not compression_survives_fork(
                        json.loads(version["input_message_ids"]),
                        prefix,
                        version_created_at=datetime.fromisoformat(version["created_at"]),
                        branch_created_at=branch.created_at,
                    ):
                        continue
                    copied = {**version, "id": f"cmp_{uuid4().hex[:16]}", "branch_id": branch_id}
                    conn.execute(
                        f"INSERT INTO compression_versions ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        tuple(copied[column] for column in columns),
                    )
                    inherited.append(copied)
                if inherited:
                    versions[branch_id] = inherited
        paths[branch_id] = prefix + own.get(branch_id, [])
        return paths[branch_id]

    for branch_id in branches:
        visit(branch_id, frozenset())
