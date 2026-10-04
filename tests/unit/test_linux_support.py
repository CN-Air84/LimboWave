"""Platform branches exercised without pretending Windows is a Linux kernel."""
from pathlib import Path

import pytest

from limbowave.domain.path_guard import InvalidPath, validate_path
from limbowave.domain.permissions import path_is_within


def test_linux_grants_are_case_sensitive() -> None:
    assert not path_is_within('/work/private/key', '/work/Private', platform='linux')
    assert path_is_within('/work/Private/key', '/work/Private', platform='linux')
    assert not path_is_within('/work/PrivateExtra/key', '/work/Private', platform='linux')
    assert not path_is_within('/work/Private/../secret', '/work/Private', platform='linux')
    assert path_is_within('/etc/hosts', '/', platform='linux')


def test_linux_backslash_is_not_a_separator() -> None:
    assert not path_is_within('/work/safe\\outside', '/work/safe', platform='linux')


def test_windows_grants_keep_windows_semantics() -> None:
    assert path_is_within('C:/WORK/Safe/file', 'c:/work/safe', platform='win32')
    assert not path_is_within('D:/work/safe', 'c:/work', platform='win32')


def test_reserved_names_are_windows_only() -> None:
    for name in ('CON', 'NUL.txt', 'C:notes'):
        assert validate_path(name, platform='linux') == Path(name)
        with pytest.raises(InvalidPath):
            validate_path(name, platform='win32')


def test_system_identity_is_not_available_on_linux() -> None:
    from limbowave.domain.platform_capabilities import supports_system_identity
    assert not supports_system_identity(platform="linux")
    assert supports_system_identity(platform="win32")


def test_linux_recovery_description_is_not_windows_account(monkeypatch) -> None:
    from limbowave.domain import platform_capabilities
    from limbowave.infrastructure.crypto.recovery import describe_protection
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: False)
    assert describe_protection() == platform_capabilities.SYSTEM_RECOVERY_UNAVAILABLE


def test_linux_identity_backend_never_authorizes(monkeypatch) -> None:
    from limbowave.domain import platform_capabilities
    from limbowave.infrastructure.crypto.recovery import hello_gate
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: False)
    gate = hello_gate()
    assert gate.name == "unavailable"
    assert not gate.available()
    assert not gate.verify("delete data")


def test_linux_reset_service_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from limbowave.application.services.data_reset_service import (
        DataResetError,
        DataResetService,
        ResetScope,
    )
    from limbowave.domain import platform_capabilities
    from tests.conftest import TEST_PASSWORD, make_vault
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: False)
    vault = tmp_path / "vault.json"
    make_vault(vault)
    before = vault.read_bytes()
    service = DataResetService(tmp_path, ResetScope.ALL)
    with pytest.raises(DataResetError, match="已禁用"):
        service.authenticate(TEST_PASSWORD)
    assert vault.read_bytes() == before
