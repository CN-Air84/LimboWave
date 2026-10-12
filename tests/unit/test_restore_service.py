"""Safe restore regression tests; only temporary data roots are touched."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from limbowave.application.services.backup_service import BackupService
from limbowave.application.services.restore_service import RestoreService
from limbowave.domain.backup import BackupError
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import KdfParams, Vault
from limbowave.infrastructure.database.migrations import CURRENT_VERSION, migrate

KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)


def library(root: Path, title: str):
    root.mkdir(parents=True, exist_ok=True)
    key = Vault(root / "vault.json", params=KDF).create("original password")
    with closing(sqlite3.connect(root / "limbowave.db")) as conn, conn:
        migrate(conn)
        conn.execute("INSERT INTO conversations (id, title_enc, created_at) VALUES (?, ?, ?)",
                     ("c1", key.encrypt(title), "2026-10-10T00:00:00+00:00"))
    SecretStore(key, root / "vault" / "secrets.json").set("api", "secret-" + title)
    return key


@pytest.fixture
def restore_case(tmp_path):
    source = tmp_path / "source"
    key = library(source, "from backup")
    archive = tmp_path / "backup.lwb"
    backup = BackupService(source, app_version="test", schema_version=CURRENT_VERSION,
                           kdf_params=KDF, vault_key=key)
    backup.create(archive, "backup password")
    root = tmp_path / "current"
    library(root, "original")
    (root / "logs").mkdir()
    (root / "logs" / "keep.log").write_text("keep")
    service = RestoreService(root, backup)
    return root, archive, service


def title(root: Path, password: str) -> str:
    key = Vault(root / "vault.json").unlock(password)
    with closing(sqlite3.connect(root / "limbowave.db")) as conn:
        return key.decrypt(conn.execute("SELECT title_enc FROM conversations").fetchone()[0])


def test_restore_preserves_old_library_and_restarts_with_backup_password(restore_case):
    root, archive, service = restore_case
    prepared = service.prepare(archive, "backup password")
    assert title(root, "original password") == "original"
    previous = prepared.execute()
    assert title(root, "backup password") == "from backup"
    assert title(previous, "original password") == "original"
    assert (root / "logs" / "keep.log").read_text() == "keep"
    key = Vault(root / "vault.json").unlock("backup password")
    assert SecretStore(key, root / "vault" / "secrets.json").get("api") == "secret-from backup"
    assert not (root / ".restore-pending.json").exists()


def test_cancel_does_not_change_live_files(restore_case):
    root, archive, service = restore_case
    prepared = service.prepare(archive, "backup password")
    prepared.cancel()
    assert title(root, "original password") == "original"
    assert not prepared.directory.exists()


@pytest.mark.parametrize("interruption", [False, True])
def test_failed_or_interrupted_install_rolls_back(restore_case, monkeypatch, interruption):
    root, archive, service = restore_case
    prepared = service.prepare(archive, "backup password")
    rename = Path.rename
    failed = False

    def fail_once(path, target):
        nonlocal failed
        if path.parent == prepared.directory / "staged" and not failed:
            failed = True
            if interruption:
                raise KeyboardInterrupt("simulated process death")
            raise OSError("simulated locked file")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_once)
    with pytest.raises(KeyboardInterrupt if interruption else BackupError):
        prepared.execute()
    monkeypatch.setattr(Path, "rename", rename)
    RestoreService.recover_pending(root)
    assert title(root, "original password") == "original"
    assert not (root / ".restore-pending.json").exists()


def test_staging_tamper_prevents_switch(restore_case):
    root, archive, service = restore_case
    prepared = service.prepare(archive, "backup password")
    (prepared.directory / "staged" / "vault.json").write_text("broken")
    with pytest.raises(BackupError):
        prepared.execute()
    assert title(root, "original password") == "original"


def test_wrong_password_does_not_leave_staging(restore_case):
    root, archive, service = restore_case
    with pytest.raises(BackupError):
        service.prepare(archive, "wrong")
    assert title(root, "original password") == "original"
    assert not list((root / "restore-history").iterdir())


def test_legacy_archive_without_key_is_not_installed(tmp_path):
    root = tmp_path / "source"
    library(root, "legacy")
    (root / "vault.json").unlink()
    backup = BackupService(root, app_version="test", schema_version=CURRENT_VERSION, kdf_params=KDF)
    archive = tmp_path / "legacy.lwb"
    backup.create(archive, "password")
    target = tmp_path / "target"
    library(target, "keep")
    with pytest.raises(BackupError, match="旧备份"):
        RestoreService(target, backup).prepare(archive, "password")
    assert title(target, "original password") == "keep"
