"""记忆文档仓库。当前列表和每轮不可变快照均使用资料库密钥加密。"""

import sqlite3

from limbowave.domain.memory import MemoryDocument
from limbowave.infrastructure.crypto.vault import VaultKey


class SqliteMemoryRepository:
    def __init__(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key

    def get(self, document_id: str) -> MemoryDocument | None:
        row = self._conn.execute(
            "SELECT id, payload, conversation_id, branch_id FROM memory_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        return MemoryDocument(row[0], self._key.decrypt(row[1]), row[2], row[3])

    def list_current(self) -> list[MemoryDocument]:
        # Exact IDs avoid decrypting snapshots, archives, policies or tool receipts.
        rows = self._conn.execute(
            "SELECT id, payload, conversation_id, branch_id FROM memory_documents "
            "WHERE id='global' OR (branch_id IS NOT NULL AND id='branch:' || branch_id) "
            "ORDER BY id"
        ).fetchall()
        return [MemoryDocument(r[0], self._key.decrypt(r[1]), r[2], r[3]) for r in rows]

    def put(self, document: MemoryDocument) -> None:
        self._conn.execute(
            "INSERT INTO memory_documents (id, payload, conversation_id, branch_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (
                document.id,
                self._key.encrypt(document.payload),
                document.conversation_id,
                document.branch_id,
            ),
        )
