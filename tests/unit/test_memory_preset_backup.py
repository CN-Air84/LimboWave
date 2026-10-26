"""真实加密备份和暂存恢复必须保留内置预设的用户选择。"""

from datetime import UTC, datetime

import pytest

from limbowave.application.services.backup_service import BackupService
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService
from limbowave.application.services.restore_service import RestoreService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.memory import MemoryRunContext
from limbowave.domain.memory_presets import BUILTIN_READER_PRESET_ID
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.vault import Vault
from limbowave.infrastructure.database.migrations import CURRENT_VERSION
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from tests.conftest import TEST_KDF, make_vault

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def _memories(root, key):
    factory = sqlite_uow_factory(root / "limbowave.db", key)
    configuration = ConfigurationService(JsonConfigRepository(root / "config.json"))
    return MemoryService(factory, configuration), factory


@pytest.mark.parametrize("choice", ["keep", "edit", "delete", "not_installed"])
def test_backup_restore_preserves_builtin_choice_and_session_customization(tmp_path, choice):
    source = tmp_path / "source"
    source.mkdir()
    key = make_vault(source / "vault.json")
    service, factory = _memories(source, key)
    archive = tmp_path / "reader.lwb"
    password = "independent reader backup password"
    context = MemoryRunContext("run", "c", "b", "message")
    try:
        with factory() as uow:
            uow.conversations.add(Conversation("c", "reader", NOW))
            uow.branches.add(Branch("b", "c", NOW))
            uow.commit()
        original = service.save("USER_GLOBAL_FROM_BACKUP")
        local = service.save("USER_BRANCH_FROM_BACKUP", "c", "b")
        if choice != "not_installed":
            assert service.ensure_builtin_preset()
            if choice == "edit":
                service.save("USER_CUSTOM_READER_FROM_BACKUP", item_id=BUILTIN_READER_PRESET_ID)
            service.save_session_global(BUILTIN_READER_PRESET_ID, "SESSION_READER_FROM_BACKUP", "c")
            if choice == "delete":
                service.delete(BUILTIN_READER_PRESET_ID)
        expected = service.list()
        expected_entries = service.list_session_global("c")
        expected_prompt = service.prompt_context(context, 1)
        backup = BackupService(
            source, app_version="test", schema_version=CURRENT_VERSION,
            kdf_params=TEST_KDF, vault_key=key,
        )
        backup.create(archive, password)
    finally:
        factory.close()
    # Neither the chosen preset text nor the user's surrounding memories leak into the archive.
    assert b"USER_CUSTOM_READER_FROM_BACKUP" not in archive.read_bytes()
    assert b"USER_GLOBAL_FROM_BACKUP" not in archive.read_bytes()

    destination = tmp_path / "destination"
    destination.mkdir()
    previous_key = make_vault(destination / "vault.json")
    previous, previous_factory = _memories(destination, previous_key)
    try:
        previous.ensure_builtin_preset()
        previous.save("DESTINATION_READER_MUST_NOT_WIN", item_id=BUILTIN_READER_PRESET_ID)
    finally:
        previous_factory.close()

    prepared = RestoreService(destination, backup).prepare(archive, password)
    prepared.execute()
    vault = Vault(destination / "vault.json")
    restored, restored_factory = _memories(destination, vault.unlock(password))
    try:
        assert restored.list() == expected
        assert restored.list_session_global("c") == expected_entries
        assert restored.prompt_context(context, 1) == expected_prompt
        assert restored.list("c", "b") == [local]
        assert restored.ensure_builtin_preset() is (choice == "not_installed")
        if choice == "not_installed":
            assert restored.list()[0] == original
            assert restored.list()[1].id == BUILTIN_READER_PRESET_ID
            assert len(restored.list()) == 2
        else:
            assert restored.list() == expected
            assert restored.prompt_context(context, 2)["global"] == expected_prompt["global"]
        assert not restored.ensure_builtin_preset()
    finally:
        restored_factory.close()
        vault.lock()
