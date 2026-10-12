"""Validated, journaled data replacement. Execute only after all writers have stopped."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from limbowave.application.services.backup_service import BackupService
from limbowave.domain.backup import BackupError, BackupInspection
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import Vault, VaultKey, backup_key_document
from limbowave.infrastructure.database.migrations import CURRENT_VERSION, current_version, migrate

# Never move the data root itself: diagnostics/runtime may still own handles in it.
OWNED = (
    "limbowave.db", "limbowave.db-wal", "limbowave.db-shm", "limbowave.db-journal",
    "config.json", "vault.json", "vault", "blobs",
)
DIRECTORIES = {"vault", "blobs"}
JOURNAL = ".restore-pending.json"
HISTORY = "restore-history"


def _no_links(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise BackupError(f"拒绝操作链接路径：{path.name}")
    if path.is_dir():
        for child in path.iterdir():
            _no_links(child)


def _fingerprint(directory: Path) -> dict[str, str]:
    _no_links(directory)
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*") if path.is_file()
    }


def _write_journal(root: Path, directory: Path, originals: list[str], phase: str) -> None:
    target = root / JOURNAL
    temporary = target.with_suffix(".tmp")
    _no_links(target)
    _no_links(temporary)
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"directory": directory.name, "originals": originals, "phase": phase}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _validate_staging(directory: Path, password: str, inspection: BackupInspection) -> None:
    if not (directory / "vault.json").is_file():
        raise BackupError("此旧备份未包含恢复密钥，不能安全自动恢复；请在原资料库中重新生成备份")
    if not (directory / "limbowave.db").is_file():
        raise BackupError("备份缺少数据库")
    vault = Vault(directory / "vault.json")
    try:
        key = vault.unlock(password)
        JsonConfigRepository(directory / "config.json").load()
        with closing(sqlite3.connect(directory / "limbowave.db")) as conn:
            conn.execute("PRAGMA trusted_schema = OFF")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("备份数据库完整性检查失败")
            if current_version(conn) != inspection.manifest.schema_version:
                raise BackupError("备份数据库版本与清单不一致")
            migrate(conn, target=CURRENT_VERSION)
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("备份数据库存在不完整的关联记录")
            # Verify encrypted columns against the restored key before touching live data.
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            for (table,) in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                for column in conn.execute(f"PRAGMA table_info({quoted})").fetchall():
                    encrypted = column[1].endswith("_enc") or (table, column[1]) in {
                        ("memory_documents", "payload"), ("request_intents", "app_params"),
                    }
                    if not encrypted:
                        continue
                    name = '"' + column[1].replace('"', '""') + '"'
                    for (value,) in conn.execute(f"SELECT {name} FROM {quoted}"):
                        if value not in (None, "", "{}", "[]"):
                            key.decrypt(value)
            for table in ("file_documents", "image_attachments"):
                references = conn.execute(f"SELECT blob_id FROM {table} WHERE blob_id IS NOT NULL")
                for (blob_id,) in references:
                    if (
                        not isinstance(blob_id, str) or len(blob_id) != 32
                        or any(c not in "0123456789abcdef" for c in blob_id)
                        or not (directory / "blobs" / f"{blob_id}.bin").is_file()
                    ):
                        raise BackupError("备份缺少数据库引用的附件")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode = DELETE")
        secrets = SecretStore(key, directory / "vault" / "secrets.json")
        for ref in secrets.refs():
            secrets.get(ref)
        blobs = BlobStore(directory / "blobs", key)
        for blob in (directory / "blobs").glob("*.bin"):
            blobs.get(blob.stem)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("备份数据库、配置或加密内容无法安全读取，当前资料库未改动") from exc
    finally:
        vault.lock()


@dataclass
class PreparedRestore:
    root: Path
    directory: Path
    inspection: BackupInspection
    fingerprints: dict[str, str]
    consumed: bool = False

    @property
    def previous(self) -> Path:
        return self.directory / "previous"

    def cancel(self) -> None:
        if self.consumed:
            return
        # Only a private, uninstalled transaction can be discarded.
        expected = self.root / HISTORY / self.directory.name
        if self.directory != expected or self.directory.resolve() != expected:
            raise BackupError("恢复临时目录路径异常")
        _no_links(self.root / HISTORY)
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.consumed = True

    def execute(self) -> Path:
        """Caller guarantees storage, IPC, background tasks and kernel are closed."""
        if self.consumed:
            raise BackupError("本次恢复已结束，请重新选择备份")
        staged = self.directory / "staged"
        _no_links(self.root / HISTORY)
        if self.directory.parent != self.root / HISTORY:
            raise BackupError("恢复目录不在指定数据目录内")
        if not staged.is_dir() or _fingerprint(staged) != self.fingerprints:
            raise BackupError("待恢复数据发生变化，请重新选择备份")
        if (self.root / JOURNAL).exists():
            raise BackupError("存在未完成恢复，请先重启应用")
        originals: list[str] = []
        for name in OWNED:
            path = self.root / name
            _no_links(path)
            if path.exists():
                if path.is_dir() != (name in DIRECTORIES):
                    raise BackupError(f"数据路径类型异常：{name}")
                originals.append(name)
        self.previous.mkdir()
        (self.directory / "discarded").mkdir()
        _write_journal(self.root, self.directory, originals, "switching")
        self.consumed = True
        try:
            for name in originals:
                (self.root / name).rename(self.previous / name)
            for path in staged.iterdir():
                if path.name not in OWNED:
                    raise BackupError("待恢复目录包含未知文件")
                path.rename(self.root / path.name)
            _write_journal(self.root, self.directory, originals, "complete")
        except Exception as exc:
            try:
                RestoreService.recover_pending(self.root)
            except Exception as rollback_error:
                raise BackupError(
                    f"恢复未完成，自动回退暂时受阻。原数据保留在 {self.previous}；"
                    "请关闭占用文件的程序后重新打开应用以重试回退。"
                ) from rollback_error
            raise BackupError("恢复失败，已自动回退到恢复前的数据") from exc
        # The complete journal is safe to leave behind if unlink fails.
        with suppress(OSError):
            (self.root / JOURNAL).unlink()
        return self.previous


class RestoreService:
    def __init__(
        self, root: Path, backup: BackupService, *, legacy_key: VaultKey | None = None,
    ) -> None:
        self.root = root.resolve()
        self.backup = backup
        self.legacy_key = legacy_key

    def prepare(self, source: Path, password: str) -> PreparedRestore:
        if (self.root / JOURNAL).exists():
            raise BackupError("存在未完成恢复，请先重启应用")
        history = self.root / HISTORY
        _no_links(history)
        history.mkdir(parents=True, exist_ok=True)
        directory = history / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid4().hex)
        directory.mkdir(mode=0o700)
        try:
            staged = directory / "staged"
            inspection = self.backup.restore_to(source, password, staged)
            if not (staged / "vault.json").exists() and self.legacy_key is not None:
                # Legacy archives did not carry a key. Only accept them if every encrypted
                # value/blob can be read with the currently unlocked library key below.
                (staged / "vault.json").write_bytes(backup_key_document(self.legacy_key, password))
                inspection = replace(inspection, notes=(*inspection.notes,
                    "旧备份未包含密钥，已用当前资料库密钥校验；备份中未保存的站点凭据需重新配置。",
                ))
            _validate_staging(staged, password, inspection)
            return PreparedRestore(self.root, directory, inspection, _fingerprint(staged))
        except BaseException:
            # directory was freshly created below the verified history root.
            _no_links(history)
            if directory.resolve().parent != history:
                raise BackupError("恢复临时目录路径异常") from None
            shutil.rmtree(directory)
            raise

    @staticmethod
    def recover_pending(root: Path) -> bool:
        """Repair a killed/failed switch before preferences, vault or SQLite are opened."""
        root = root.resolve()
        journal = root / JOURNAL
        _no_links(journal)
        if not journal.exists():
            return False
        data = json.loads(journal.read_text(encoding="utf-8"))
        name = data["directory"]
        originals = data["originals"]
        if (
            not isinstance(name, str) or not name or any(c not in "0123456789abcdef-" for c in name)
            or not isinstance(originals, list) or any(item not in OWNED for item in originals)
            or data["phase"] not in {"switching", "complete"}
        ):
            raise BackupError("恢复记录异常，未改动数据")
        _no_links(root / HISTORY)
        directory = root / HISTORY / name
        if not directory.is_dir():
            raise BackupError("恢复回退目录缺失，未改动数据")
        if data["phase"] == "complete":
            journal.unlink()
            return False
        previous = directory / "previous"
        discarded = directory / "discarded"
        if not previous.is_dir() or not discarded.is_dir():
            raise BackupError("恢复回退目录不完整，未改动数据")
        for item in OWNED:
            _no_links(root / item)
        for item in OWNED:
            live = root / item
            old = previous / item
            if old.exists() or item not in originals:
                if live.exists():
                    live.rename(discarded / (item + "-" + uuid4().hex))
                if old.exists():
                    old.rename(live)
        journal.unlink()
        return True
