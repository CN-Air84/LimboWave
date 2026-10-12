"""加密备份与恢复（Phase 8 / 设计计划 §十四.3）。

**创建**：收集资料库的权威内容 → 逐文件 sha256 → 写清单 → 打成 tar
→ 用**独立备份密码**派生的密钥加密落盘。

**恢复**（§十四.3 的五个前置动作，顺序固定）：

1. 验证包完整性（逐文件哈希 + AEAD tag）；
2. 显示版本和内容统计（返回 :class:`BackupInspection` 供 UI 展示）；
3. 检查迁移兼容性（备份 schema 不得高于当前应用）；
4. **先恢复到临时目录**；
5. 成功校验后再切换资料库。

本服务只做 1–4；第 5 步（切换）由调用方决定——因为「切换」意味着动用户的
真实资料库目录，不该藏在服务内部自动发生。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from limbowave.domain.backup import (
    BackupError,
    BackupInspection,
    BackupManifest,
    EntryHash,
    IntegrityError,
    check_compatibility,
)
from limbowave.infrastructure.backup.archive import read_archive, read_header, write_archive
from limbowave.infrastructure.crypto.vault import KdfParams, VaultKey, backup_key_document

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "limbowave.db"
CONFIG_NAME = "config.json"
BLOB_PREFIX = "blobs/"

# 备份包内固定路径
ARCHIVE_DATABASE = DATABASE_NAME
ARCHIVE_CONFIG = CONFIG_NAME


@dataclass(frozen=True, slots=True)
class BackupResult:
    """一次备份的结果。"""

    path: Path
    manifest: BackupManifest


class BackupService:
    """创建与恢复加密备份。"""

    def __init__(
        self,
        data_root: Path,
        *,
        app_version: str,
        schema_version: int,
        kdf_params: KdfParams | None = None,
        vault_key: VaultKey | None = None,
    ) -> None:
        self._root = data_root.resolve()
        self._app_version = app_version
        self._schema_version = schema_version
        # 备份 KDF 参数可独立于资料库（备份可能在别的机器上解开）
        self._kdf = kdf_params
        self._vault_key = vault_key

    # ---------- 创建 ----------

    def create(self, target: Path, password: str) -> BackupResult:
        """生成加密备份包。``password`` 是**独立备份密码**（不是资料库主密码）。"""
        if not password:
            raise BackupError("备份密码不得为空")
        files = self._collect()
        if self._vault_key is not None:
            files["vault.json"] = backup_key_document(self._vault_key, password, self._kdf)
        elif (self._root / "vault.json").exists():
            raise BackupError("资料库未解锁，无法创建包含恢复密钥的备份")
        manifest = self._build_manifest(files)
        payload = {MANIFEST_NAME: _dump_manifest(manifest), **files}
        write_archive(target, payload, password, params=self._kdf)
        return BackupResult(path=target, manifest=manifest)

    def _collect(self) -> dict[str, bytes]:
        """收集要备份的文件。缺失的项跳过（如还没配置站点）。"""
        files: dict[str, bytes] = {}
        database = self._root / DATABASE_NAME
        if database.is_file():
            # A raw file copy loses committed pages still in the live WAL.
            with tempfile.TemporaryDirectory() as tmp:
                snapshot = Path(tmp) / DATABASE_NAME
                with (
                    closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn,
                    closing(sqlite3.connect(snapshot)) as target,
                ):
                    conn.backup(target)
                files[ARCHIVE_DATABASE] = snapshot.read_bytes()
        config = self._root / CONFIG_NAME
        if config.is_file():
            files[ARCHIVE_CONFIG] = config.read_bytes()
        secrets = self._root / "vault" / "secrets.json"
        if secrets.is_file():
            files["vault/secrets.json"] = secrets.read_bytes()
        blobs_dir = self._root / "blobs"
        if blobs_dir.is_dir():
            for blob in sorted(blobs_dir.glob("*.bin")):
                files[f"{BLOB_PREFIX}{blob.name}"] = blob.read_bytes()
        return files

    def _build_manifest(self, files: dict[str, bytes]) -> BackupManifest:
        entries = tuple(
            EntryHash(
                path=name,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
            for name, content in sorted(files.items())
        )
        stats = self._content_stats(files)
        return BackupManifest(
            format_version=1,
            schema_version=self._schema_version,
            app_version=self._app_version,
            created_at=datetime.now(UTC),
            entries=entries,
            conversation_count=stats["conversations"],
            message_count=stats["messages"],
            blob_count=sum(1 for name in files if name.startswith(BLOB_PREFIX)),
            grant_count=stats["grants"],
        )

    def _content_stats(self, files: dict[str, bytes]) -> dict[str, int]:
        """从备份的数据库里数出内容统计（只读，不碰真实资料库）。"""
        raw = files.get(ARCHIVE_DATABASE)
        if raw is None:
            return {"conversations": 0, "messages": 0, "grants": 0}
        import sqlite3
        import tempfile

        # 写临时文件让 sqlite 打开（备份里的库可能不是 WAL 状态）
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / DATABASE_NAME
            candidate.write_bytes(raw)
            try:
                conn = sqlite3.connect(str(candidate))
                try:
                    return {
                        "conversations": _count(conn, "conversations"),
                        "messages": _count(conn, "messages"),
                        "grants": _count(conn, "permission_grants"),
                    }
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                return {"conversations": 0, "messages": 0, "grants": 0}

    # ---------- 检查（恢复前） ----------

    def inspect(self, source: Path, password: str) -> BackupInspection:
        """解密、校验完整性、检查兼容性。**不落任何文件**。"""
        _, files = read_archive(source, password)
        return self._inspect_files(files)

    def _inspect_files(self, files: dict[str, bytes]) -> BackupInspection:
        raw_manifest = files.get(MANIFEST_NAME)
        if raw_manifest is None:
            raise BackupError("备份包缺少清单文件")
        manifest = _load_manifest(raw_manifest)

        problems = _verify_entries(manifest, files)
        notes: list[str] = []
        compatible = True
        try:
            check_compatibility(manifest, self._schema_version)
        except BackupError as exc:
            compatible = False
            notes.append(str(exc))

        return BackupInspection(
            manifest=manifest,
            integrity_ok=not problems,
            compatible=compatible,
            integrity_problems=tuple(problems),
            notes=tuple(notes),
        )

    # ---------- 恢复 ----------

    def restore_to(self, source: Path, password: str, destination: Path) -> BackupInspection:
        """把备份恢复到 ``destination``（**应当是临时目录**，§十四.3 第 4 步）。

        先校验再落盘：完整性或兼容性不过就**一个字节都不写**。
        切换资料库是调用方的事（第 5 步）——那要动用户的真实目录，
        不该由服务悄悄做。
        """
        _, files = read_archive(source, password)
        inspection = self._inspect_files(files)
        if not inspection.integrity_ok:
            raise IntegrityError("备份完整性校验失败：" + "; ".join(inspection.integrity_problems))
        if not inspection.compatible:
            raise BackupError("备份与当前应用不兼容：" + "; ".join(inspection.notes))

        if destination.exists() and (destination.is_symlink() or any(destination.iterdir())):
            raise BackupError("恢复目标必须是空的临时目录")
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            if name == MANIFEST_NAME:
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return inspection


def _count(conn: object, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # type: ignore[attr-defined]
    except Exception:
        return 0
    return int(row[0]) if row else 0


def _dump_manifest(manifest: BackupManifest) -> bytes:
    payload = {
        "format_version": manifest.format_version,
        "schema_version": manifest.schema_version,
        "app_version": manifest.app_version,
        "created_at": manifest.created_at.isoformat(),
        "entries": [{"path": e.path, "sha256": e.sha256, "size": e.size} for e in manifest.entries],
        "stats": {
            "conversations": manifest.conversation_count,
            "messages": manifest.message_count,
            "blobs": manifest.blob_count,
            "grants": manifest.grant_count,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _load_manifest(raw: bytes) -> BackupManifest:
    data = json.loads(raw.decode("utf-8"))
    stats = data.get("stats") or {}
    return BackupManifest(
        format_version=int(data["format_version"]),
        schema_version=int(data["schema_version"]),
        app_version=str(data.get("app_version", "unknown")),
        created_at=datetime.fromisoformat(data["created_at"]),
        entries=tuple(
            EntryHash(path=e["path"], sha256=e["sha256"], size=int(e["size"]))
            for e in data.get("entries") or []
        ),
        conversation_count=int(stats.get("conversations", 0)),
        message_count=int(stats.get("messages", 0)),
        blob_count=int(stats.get("blobs", 0)),
        grant_count=int(stats.get("grants", 0)),
    )


def _verify_entries(manifest: BackupManifest, files: dict[str, bytes]) -> list[str]:
    """逐文件校验哈希。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    names = [entry.path for entry in manifest.entries]
    if len(set(names)) != len(names):
        problems.append("清单含重复路径")
    if set(names) != set(files) - {MANIFEST_NAME}:
        problems.append("清单与归档文件不一致")
    for name in names:
        parts = name.split("/")
        allowed = name in {DATABASE_NAME, CONFIG_NAME, "vault.json", "vault/secrets.json"}
        allowed = allowed or (
            len(parts) == 2 and parts[0] == "blobs" and parts[1].endswith(".bin")
        )
        if not allowed:
            problems.append(f"不支持的备份路径：{name}")
    for entry in manifest.entries:
        content = files.get(entry.path)
        if content is None:
            problems.append(f"缺少文件：{entry.path}")
            continue
        if len(content) != entry.size:
            problems.append(f"大小不符：{entry.path}")
            continue
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            problems.append(f"哈希不符：{entry.path}")
    return problems


__all__ = ["BackupResult", "BackupService", "read_header"]
