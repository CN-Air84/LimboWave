"""重置只在双重验证、等待和最终确认后删除；所有目标均在临时目录。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from limbowave.application.services import data_reset_service as reset
from limbowave.application.services.data_reset_service import (
    DATABASE_FILES,
    RESET_DIRECTORIES,
    RESET_FILES,
    DataResetError,
    DataResetService,
    ResetScope,
)
from limbowave.infrastructure.crypto.recovery import NoHelloGate
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault, VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from tests.conftest import TEST_PASSWORD


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(reset, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def hello_gate() -> Mock:
    gate = Mock()
    gate.name = "windows-hello"
    gate.available.return_value = True
    gate.verify.return_value = True
    return gate


def populate(root: Path) -> dict[str, bytes]:
    for name in DATABASE_FILES:
        (root / name).write_bytes(b"database fixture")
    for name in RESET_FILES:
        if name != "vault.json":
            (root / name).write_bytes(b"settings fixture")
    for name in RESET_DIRECTORIES:
        (root / name).mkdir()
        (root / name / "owned.bin").write_bytes(b"encrypted fixture")
    for name in ("backup.lwb", "export.html", "notes.txt"):
        (root / name).write_bytes(b"keep")
    return snapshot(root)


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def approve(service: DataResetService, clock: list[float]) -> None:
    service.authenticate(TEST_PASSWORD, gate=hello_gate())
    service.begin_confirmation()
    clock[0] += 5
    service.confirm()


@pytest.mark.parametrize("scope", list(ResetScope))
def test_selected_scope_is_deleted_and_backups_preserved(
    tmp_path: Path, vault_key: VaultKey, clock: list[float], scope: ResetScope,
) -> None:
    before = populate(tmp_path)
    service = DataResetService(tmp_path, scope)
    approve(service, clock)
    assert snapshot(tmp_path) == before  # 最终确认本身仍不删除，先由应用关闭资源。
    service.execute()
    after = snapshot(tmp_path)
    if scope is ResetScope.DATABASE:
        assert after == {name: raw for name, raw in before.items() if name not in DATABASE_FILES}
        assert Vault(tmp_path / "vault.json").unlock(TEST_PASSWORD)
    else:
        assert after == dict.fromkeys(("backup.lwb", "export.html", "notes.txt"), b"keep")
    with pytest.raises(DataResetError):
        service.execute()


def test_wrong_master_password_never_calls_hello(
    tmp_path: Path, vault_key: VaultKey,
) -> None:
    before = populate(tmp_path)
    gate = hello_gate()
    service = DataResetService(tmp_path, ResetScope.ALL)
    with pytest.raises(InvalidPassword):
        service.authenticate("wrong", gate=gate)
    gate.verify.assert_not_called()
    with pytest.raises(DataResetError):
        service.execute()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("failure", ["unavailable", "cancelled", "exception", "account-fallback"])
def test_hello_must_actually_verify(
    tmp_path: Path, vault_key: VaultKey, failure: str,
) -> None:
    before = populate(tmp_path)
    gate = hello_gate()
    if failure == "unavailable":
        gate.available.return_value = False
    elif failure == "cancelled":
        gate.verify.return_value = False
    elif failure == "exception":
        gate.verify.side_effect = RuntimeError("system unavailable")
    else:
        gate = NoHelloGate()
    service = DataResetService(tmp_path, ResetScope.DATABASE)
    with pytest.raises((DataResetError, RuntimeError)):
        service.authenticate(TEST_PASSWORD, gate=gate)
    with pytest.raises(DataResetError):
        service.begin_confirmation()
    with pytest.raises(DataResetError):
        service.execute()
    assert snapshot(tmp_path) == before


def test_confirmation_requires_full_five_seconds_after_verification(
    tmp_path: Path, vault_key: VaultKey, clock: list[float],
) -> None:
    service = DataResetService(tmp_path, ResetScope.DATABASE)
    with pytest.raises(DataResetError):
        service.confirm()
    with pytest.raises(DataResetError):
        service.execute()
    service.authenticate(TEST_PASSWORD, gate=hello_gate())
    clock[0] += 90  # 花在系统 PIN 弹窗或调度上的时间不能抵扣倒计时。
    service.begin_confirmation()
    clock[0] += 4.999
    with pytest.raises(DataResetError, match="5 秒"):
        service.confirm()
    clock[0] += 0.001
    service.confirm()


@pytest.mark.parametrize("stage", ["before", "during", "waiting", "confirmed"])
def test_cancel_revokes_authorization_in_every_stage(
    tmp_path: Path, vault_key: VaultKey, clock: list[float], stage: str,
) -> None:
    before = populate(tmp_path)
    service = DataResetService(tmp_path, ResetScope.ALL)
    if stage == "before":
        service.cancel()
        with pytest.raises(DataResetError):
            service.authenticate(TEST_PASSWORD, gate=hello_gate())
    elif stage == "during":
        gate = hello_gate()
        gate.verify.side_effect = lambda _reason: (service.cancel(), True)[1]
        with pytest.raises(DataResetError):
            service.authenticate(TEST_PASSWORD, gate=gate)
    else:
        service.authenticate(TEST_PASSWORD, gate=hello_gate())
        service.begin_confirmation()
        if stage == "confirmed":
            clock[0] += 5
            service.confirm()
        service.cancel()
    clock[0] += 10
    with pytest.raises(DataResetError):
        service.execute()
    assert snapshot(tmp_path) == before


def test_preflight_rejects_unexpected_directory_without_deleting_database(
    tmp_path: Path, vault_key: VaultKey, clock: list[float],
) -> None:
    (tmp_path / "limbowave.db").write_bytes(b"keep")
    (tmp_path / "blobs").write_bytes(b"not a directory")
    before = snapshot(tmp_path)
    service = DataResetService(tmp_path, ResetScope.ALL)
    approve(service, clock)
    with pytest.raises(DataResetError, match="路径类型异常"):
        service.execute()
    assert snapshot(tmp_path) == before


def test_partial_deletion_reports_failure_and_preserves_master_key(
    tmp_path: Path, vault_key: VaultKey, clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    populate(tmp_path)
    service = DataResetService(tmp_path, ResetScope.ALL)
    approve(service, clock)
    unlink = Path.unlink

    def locked(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "limbowave.db-wal":
            raise PermissionError("locked")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(DataResetError, match="重置未完成") as error:
        service.execute()
    assert "已删除：limbowave.db" in str(error.value)
    assert (tmp_path / "vault.json").exists()
    assert (tmp_path / "blobs" / "owned.bin").exists()
    with pytest.raises(DataResetError):
        service.execute()


def test_real_database_connections_are_released_and_cannot_recreate_database(
    tmp_path: Path, vault_key: VaultKey, clock: list[float],
) -> None:
    path = tmp_path / "limbowave.db"
    factory = sqlite_uow_factory(path, vault_key)
    with factory() as unit:
        unit.conversations.list_all()
    with pytest.raises(sqlite3.ProgrammingError):
        unit.conversations.list_all()
    assert not factory._units
    # 即使有调用者漏掉上下文管理器，工厂关闭时也必须释放它持有的连接。
    retained = factory()
    factory()  # 故意不保留引用，不允许依赖 GC 才关库。
    factory.close()
    with pytest.raises(sqlite3.ProgrammingError):
        retained.conversations.list_all()
    service = DataResetService(tmp_path, ResetScope.DATABASE)
    approve(service, clock)
    service.execute()
    with pytest.raises(RuntimeError, match="已关闭"):
        factory()
    assert not path.exists()
    assert not factory._units


def test_transaction_failure_also_releases_owned_connection(
    tmp_path: Path, vault_key: VaultKey,
) -> None:
    factory = sqlite_uow_factory(tmp_path / "limbowave.db", vault_key)
    with pytest.raises(ValueError, match="test"), factory() as unit:
        raise ValueError("test")
    assert not factory._units
    with pytest.raises(sqlite3.ProgrammingError):
        unit.conversations.list_all()
    factory.close()
