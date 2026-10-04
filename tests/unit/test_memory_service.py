"""双后端记忆契约、历史分叉和专用授权。"""

from datetime import UTC, datetime

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService, capture_memory, fork_memory
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.memory import MemoryDocument, MemoryPolicy, MemoryRunContext, MemorySettings
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

NOW = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def memory_stack(request, tmp_path, vault_key):
    factory = (
        sqlite_uow_factory(tmp_path / "memory.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory()
    )
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", NOW))
        uow.branches.add(Branch("b", "c", NOW))
        uow.branches.add(Branch("other", "c", NOW))
        uow.commit()
    service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    yield service, factory
    if hasattr(factory, "close"):
        factory.close()


def test_configuration_boundaries(memory_stack):
    service, _ = memory_stack
    assert service.settings() == MemorySettings()
    assert service.settings().global_interval == service.settings().session_interval == 15
    service.save_settings(MemorySettings(global_interval=1, session_interval=30))
    assert service.settings().session_interval == 30
    for value in (0, 31, True, 2.5, "15"):
        with pytest.raises(ValueError):
            MemorySettings(global_interval=value)
    with pytest.raises(ValueError):
        MemorySettings(default_policy="inherit")


def test_crud_promotion_and_scope(memory_stack):
    service, _ = memory_stack
    item = service.save("中文记忆", "c", "b")
    assert service.list("c", "other") == []
    with pytest.raises(ValueError):
        service.delete(item.id, "c", "other")
    global_item = service.promote(item.id, "c", "b")
    assert service.promote(item.id, "c", "b") == global_item
    service.save("new", "c", "b", item_id=item.id)
    assert service.list()[0].content == "中文记忆"
    service.delete(item.id, "c", "b")
    assert not service.list("c", "b")
    assert service.list() == [global_item]


def test_fork_uses_start_snapshot_not_future_edits(memory_stack):
    service, factory = memory_stack
    item = service.save("before", "c", "b")
    with factory() as uow:
        capture_memory(uow, "message", "c", "b")
        uow.commit()
    service.save("future edit", "c", "b", item_id=item.id)
    service.delete(item.id, "c", "b")
    service.save("future addition", "c", "b")
    with factory() as uow:
        fork_memory(uow, "message", "c", "other")
        uow.commit()
    assert service.list("c", "other") == [item]
    service.save("child edit", "c", "other", item_id=item.id)
    assert service.list("c", "b")[0].content == "future addition"


def test_snapshot_and_writes_rollback(memory_stack):
    _service, factory = memory_stack
    with factory() as uow:
        uow.memories.put(MemoryDocument("global", "[]"))
        capture_memory(uow, "not-committed", "c", "b")
    with factory() as uow:
        assert uow.memories.get("global") is None
        assert uow.memories.get("snapshot:not-committed") is None


def test_dynamic_policy_and_override(memory_stack):
    service, _ = memory_stack
    assert service.policy("c") is MemoryPolicy.INHERIT
    assert service.effective_policy("c") is MemoryPolicy.ASK
    service.save_settings(MemorySettings(default_policy="allow"))
    assert service.effective_policy("c") is MemoryPolicy.ALLOW
    service.set_policy("c", MemoryPolicy.ASK)
    assert service.effective_policy("c") is MemoryPolicy.ASK
    service.set_policy("c", MemoryPolicy.INHERIT)
    assert service.effective_policy("c") is MemoryPolicy.ALLOW


def test_model_requires_exact_live_authorization_and_is_idempotent(memory_stack):
    service, _ = memory_stack
    context = MemoryRunContext("run", "c", "b", "msg")

    def active(c):
        return c == context

    with pytest.raises(ValueError):
        service.add_from_model(context, "call", "keep", active)
    service.approve(context, "call", "keep")
    with pytest.raises(ValueError):
        service.add_from_model(context, "call", "changed", active)
    service.approve(context, "call", "keep")
    item = service.add_from_model(context, "call", "keep", active)
    assert service.add_from_model(context, "call", "keep", active) == item
    service.delete(item.id, "c", "b")
    service.add_from_model(context, "call", "keep", active)
    assert not service.list("c", "b")
    service.approve(context, "late", "late")
    with pytest.raises(ValueError):
        service.add_from_model(context, "late", "late", lambda _: False)
    assert not service.list()


def test_delete_conversation_keeps_promoted_global(memory_stack):
    service, factory = memory_stack
    item = service.save("keep global", "c", "b")
    service.promote(item.id, "c", "b")
    with factory() as uow:
        capture_memory(uow, "msg", "c", "b")
        uow.conversations.delete("c")
        uow.commit()
    with factory() as uow:
        assert uow.memories.get("branch:b") is None
        assert uow.memories.get("snapshot:msg") is None
    assert service.list()[0].content == "keep global"


def test_database_encrypts_snapshots_and_survives_reopen(tmp_path, vault_key):
    factory = sqlite_uow_factory(tmp_path / "sealed.db", vault_key)
    config = ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    service = MemoryService(factory, config)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", NOW))
        uow.branches.add(Branch("b", "c", NOW))
        uow.commit()
    item = service.save("SECRET_MEMORY_UNIQUE", "c", "b")
    with factory() as uow:
        capture_memory(uow, "msg", "c", "b")
        uow.commit()
    factory.close()
    assert b"SECRET_MEMORY_UNIQUE" not in (tmp_path / "sealed.db").read_bytes()
    reopened = sqlite_uow_factory(tmp_path / "sealed.db", vault_key)
    try:
        assert MemoryService(reopened, config).list("c", "b") == [item]
        with reopened() as uow:
            assert "SECRET_MEMORY_UNIQUE" in uow.memories.get("snapshot:msg").payload
    finally:
        reopened.close()


def test_other_settings_preserve_memory_configuration(memory_stack):
    from limbowave.application.services.settings_service import _rebuild
    from limbowave.domain.configuration import AppConfiguration

    config = AppConfiguration(
        memory=MemorySettings(global_interval=2, session_interval=7, default_policy="allow")
    )
    assert _rebuild(config, models=[]).memory == config.memory
