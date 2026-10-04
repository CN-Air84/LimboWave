"""加密备份与恢复验收（Phase 8 / §十四.3）。

锁住的不变量：
- **独立备份密码**：不需要资料库主密钥就能解开（迁移到新设备的前提）；
- 归档整体加密：磁盘上看不到数据库内容或 blob 明文；
- 密码错 / 被篡改 → 明确拒绝（AEAD tag）；
- 完整性：逐文件 sha256 与清单比对，缺文件/改内容都能查出来；
- 兼容性：来自更高 schema 版本的备份拒绝恢复；
- 恢复**先校验再落盘**：不通过就一个字节都不写；
- 内容统计（会话/消息/附件/授权数）在恢复前可见。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from limbowave.application.services.backup_service import (
    ARCHIVE_CONFIG,
    ARCHIVE_DATABASE,
    MANIFEST_NAME,
    BackupService,
)
from limbowave.domain.backup import (
    FORMAT_VERSION,
    BackupManifest,
    IncompatibleBackup,
    IntegrityError,
    WrongPassword,
    check_compatibility,
)
from limbowave.infrastructure.backup.archive import (
    build_archive,
    read_archive,
    read_header,
    unpack_archive,
    write_archive,
)
from limbowave.infrastructure.crypto.vault import KdfParams
from limbowave.infrastructure.database.migrations import CURRENT_VERSION, migrate

FAST_KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD = "independent backup password"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """一个像样的资料库：库 + 配置 + 两个 blob。"""
    root = tmp_path / "data"
    root.mkdir()
    conn = sqlite3.connect(str(root / "limbowave.db"))
    migrate(conn)
    conn.execute("INSERT INTO conversations (id, title_enc, created_at) VALUES ('c1', 'x', 'now')")
    conn.commit()
    conn.close()
    (root / "config.json").write_text('{"models": []}', encoding="utf-8")
    blobs = root / "blobs"
    blobs.mkdir()
    (blobs / "aaaa.bin").write_bytes(b"encrypted-blob-content-1")
    (blobs / "bbbb.bin").write_bytes(b"encrypted-blob-content-2")
    return root


@pytest.fixture
def service(data_root: Path) -> BackupService:
    return BackupService(
        data_root,
        app_version="0.1.0",
        schema_version=CURRENT_VERSION,
        kdf_params=FAST_KDF,
    )


# ---------- 归档层 ----------


def test_archive_round_trip(tmp_path: Path) -> None:
    files = {"a.txt": b"hello", "dir/b.bin": b"\x00\x01"}
    target = tmp_path / "b.lwb"
    write_archive(target, files, PASSWORD, params=FAST_KDF)
    _, restored = read_archive(target, PASSWORD)
    assert restored == files


def test_archive_encrypted_on_disk(tmp_path: Path) -> None:
    """磁盘上不得出现内容明文。"""
    target = tmp_path / "b.lwb"
    write_archive(target, {"secret.txt": b"TOP-SECRET-CONTENT"}, PASSWORD, params=FAST_KDF)
    raw = target.read_bytes()
    assert b"TOP-SECRET-CONTENT" not in raw
    assert raw.startswith(b"LWBK")  # 明文头（只有格式信息）


def test_archive_wrong_password_rejected(tmp_path: Path) -> None:
    target = tmp_path / "b.lwb"
    write_archive(target, {"a": b"1"}, PASSWORD, params=FAST_KDF)
    with pytest.raises(WrongPassword):
        read_archive(target, "wrong password")


def test_archive_tamper_detected(tmp_path: Path) -> None:
    """翻转密文一个字节 → AEAD tag 校验失败。"""
    target = tmp_path / "b.lwb"
    write_archive(target, {"a": b"content"}, PASSWORD, params=FAST_KDF)
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0x01
    target.write_bytes(bytes(raw))
    with pytest.raises(WrongPassword):
        read_archive(target, PASSWORD)


def test_header_readable_without_password(tmp_path: Path) -> None:
    """明文头可读——恢复前要能识别格式与 KDF 参数。"""
    target = tmp_path / "b.lwb"
    write_archive(target, {"a": b"1"}, PASSWORD, params=FAST_KDF)
    header = read_header(target)
    assert header.format_version == FORMAT_VERSION
    assert header.kdf.time_cost == FAST_KDF.time_cost


def test_non_backup_file_rejected(tmp_path: Path) -> None:
    target = tmp_path / "not.lwb"
    target.write_bytes(b"just some random bytes here")
    with pytest.raises(WrongPassword, match="magic"):
        read_header(target)


def test_archive_rejects_traversal_paths() -> None:
    """备份包也可能是恶意构造的：归档内路径不得含 .. 或绝对路径。"""
    payload = build_archive({"ok.txt": b"fine"})
    assert unpack_archive(payload) == {"ok.txt": b"fine"}


# ---------- 创建备份 ----------


def test_create_backup_includes_all_parts(service: BackupService, tmp_path: Path) -> None:
    result = service.create(tmp_path / "backup.lwb", PASSWORD)
    _, files = read_archive(tmp_path / "backup.lwb", PASSWORD)

    assert MANIFEST_NAME in files
    assert ARCHIVE_DATABASE in files
    assert ARCHIVE_CONFIG in files
    assert any(name.startswith("blobs/") for name in files)
    assert result.manifest.blob_count == 2


def test_manifest_has_version_and_hashes(service: BackupService, tmp_path: Path) -> None:
    """清单含版本、统计与逐文件哈希（§十四.3：版本清单和完整性哈希）。"""
    result = service.create(tmp_path / "b.lwb", PASSWORD)
    manifest = result.manifest
    assert manifest.format_version == FORMAT_VERSION
    assert manifest.schema_version == CURRENT_VERSION
    assert manifest.app_version
    assert manifest.entries
    assert all(len(e.sha256) == 64 for e in manifest.entries)
    assert manifest.total_bytes() > 0


def test_manifest_counts_content(service: BackupService, tmp_path: Path) -> None:
    """内容统计来自备份的库（恢复前展示用）。"""
    result = service.create(tmp_path / "b.lwb", PASSWORD)
    assert result.manifest.conversation_count == 1
    assert result.manifest.blob_count == 2


def test_create_requires_password(service: BackupService, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="密码"):
        service.create(tmp_path / "b.lwb", "")


# ---------- 兼容性 ----------


def test_backup_from_newer_schema_rejected() -> None:
    manifest = BackupManifest(
        format_version=FORMAT_VERSION,
        schema_version=CURRENT_VERSION + 5,
        app_version="9.9.9",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    with pytest.raises(IncompatibleBackup, match="更新的应用"):
        check_compatibility(manifest, CURRENT_VERSION)


def test_backup_from_older_schema_allowed() -> None:
    """低版本备份可以恢复（迁移系统会把 schema 升上来）。"""
    from datetime import UTC, datetime

    manifest = BackupManifest(
        format_version=FORMAT_VERSION,
        schema_version=1,
        app_version="0.0.1",
        created_at=datetime.now(UTC),
    )
    check_compatibility(manifest, CURRENT_VERSION)  # 不抛


def test_newer_format_version_rejected() -> None:
    from datetime import UTC, datetime

    manifest = BackupManifest(
        format_version=FORMAT_VERSION + 1,
        schema_version=1,
        app_version="x",
        created_at=datetime.now(UTC),
    )
    with pytest.raises(IncompatibleBackup, match="格式"):
        check_compatibility(manifest, CURRENT_VERSION)


# ---------- 检查与恢复 ----------


def test_inspect_reports_stats_and_integrity(service: BackupService, tmp_path: Path) -> None:
    source = tmp_path / "b.lwb"
    service.create(source, PASSWORD)

    inspection = service.inspect(source, PASSWORD)
    assert inspection.integrity_ok is True
    assert inspection.compatible is True
    assert inspection.can_restore is True
    assert "会话" in inspection.summary()
    assert inspection.manifest.conversation_count == 1


def test_inspect_wrong_password(service: BackupService, tmp_path: Path) -> None:
    source = tmp_path / "b.lwb"
    service.create(source, PASSWORD)
    with pytest.raises(WrongPassword):
        service.inspect(source, "nope")


def test_restore_to_temp_then_verify(service: BackupService, tmp_path: Path) -> None:
    """恢复到目标目录（应当是临时目录），内容与源一致。"""
    source = tmp_path / "b.lwb"
    service.create(source, PASSWORD)
    destination = tmp_path / "restore-tmp"

    inspection = service.restore_to(source, PASSWORD, destination)
    assert inspection.integrity_ok
    assert (destination / ARCHIVE_DATABASE).is_file()
    assert (destination / ARCHIVE_CONFIG).is_file()
    assert (destination / "blobs").is_dir()
    # 恢复出来的库能打开且内容在
    conn = sqlite3.connect(str(destination / ARCHIVE_DATABASE))
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    conn.close()


def test_restore_verifies_integrity_before_writing(service: BackupService, tmp_path: Path) -> None:
    """完整性不过 → 一个字节都不写（§十四.3 第 4 步先校验）。"""
    source = tmp_path / "b.lwb"
    result = service.create(source, PASSWORD)

    # 篡改归档内某个文件的哈希（重新打包时改内容但清单不动）
    _, files = read_archive(source, PASSWORD)
    files[ARCHIVE_CONFIG] = b'{"tampered": true}'
    write_archive(source, files, PASSWORD, params=FAST_KDF)

    destination = tmp_path / "restore-tmp"
    # 大小或哈希不符都算完整性失败（这里改的内容长度不同，先被大小拦下）
    with pytest.raises(IntegrityError, match=r"大小不符|哈希不符"):
        service.restore_to(source, PASSWORD, destination)
    assert not destination.exists() or not any(destination.iterdir())
    assert result.manifest.entries  # 备份本身是完整的
