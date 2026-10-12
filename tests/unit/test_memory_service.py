"""双后端记忆契约、历史分叉和专用授权。"""

from datetime import UTC, datetime
from functools import partial

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


def _second_conversation(factory):
    with factory() as uow:
        uow.conversations.add(Conversation("second", "chat", NOW))
        uow.branches.add(Branch("second-branch", "second", NOW))
        uow.commit()


def test_session_global_edit_delete_restore_are_isolated(memory_stack):
    service, factory = memory_stack
    _second_conversation(factory)
    first = service.save("全局原文")
    second = service.save("要在本会话删除")
    local = service.save("分支记忆", "c", "b")
    service.save_session_global(first.id, " 本会话版本 ", "c")
    service.delete_session_global(second.id, "c")
    entries = service.list_session_global("c")
    assert [(e.memory.content, e.state) for e in entries] == [
        ("本会话版本", "modified"), (second.content, "deleted")
    ]
    assert [i.content for i in service.effective_global("c")] == ["本会话版本"]
    assert service.effective_global("second") == [first, second]
    assert service.list() == [first, second]
    assert service.list("c", "b") == [local]
    for branch in ("b", "other"):
        context = service.prompt_context(MemoryRunContext("run", "c", branch, "msg"), 1)
        assert context["global"] == ["本会话版本"]
    service.restore_session_global(first.id, "c")
    service.restore_session_global(second.id, "c")
    assert service.effective_global("c") == [first, second]
    assert all(e.state == "inherited" for e in service.list_session_global("c"))


def test_session_global_inherits_updates_except_overrides(memory_stack):
    service, _ = memory_stack
    modified = service.save("原文")
    deleted = service.save("删除项")
    inherited = service.save("继承项")
    service.save_session_global(modified.id, "自定义", "c")
    service.delete_session_global(deleted.id, "c")
    for item in (modified, deleted, inherited):
        service.save("新版" + item.content, item_id=item.id)
    added = service.save("新增全局")
    assert [i.content for i in service.effective_global("c")] == [
        "自定义", "新版继承项", added.content
    ]
    service.restore_session_global(modified.id, "c")
    assert service.effective_global("c")[0].content == "新版原文"
    service.delete(modified.id)
    service.delete(deleted.id)
    assert [e.memory.id for e in service.list_session_global("c")] == [inherited.id, added.id]


def test_session_global_delete_edited_item_and_reedit_deleted_item(memory_stack):
    service, _ = memory_stack
    item = service.save("original")
    service.save_session_global(item.id, "custom", "c")
    service.delete_session_global(item.id, "c")
    assert service.effective_global("c") == []
    service.save_session_global(item.id, "custom again", "c")
    assert service.effective_global("c")[0].content == "custom again"
    service.restore_session_global(item.id, "c")
    service.restore_session_global(item.id, "c")
    assert service.effective_global("c") == [item]


def test_session_global_validates_scope_and_content(memory_stack):
    service, _ = memory_stack
    item = service.save("original")
    local = service.save("local", "c", "b")
    for conversation in ("", "missing"):
        with pytest.raises(ValueError, match="会话"):
            service.list_session_global(conversation)
    for item_id, conversation in ((item.id, "missing"), (local.id, "c"), ("missing", "c")):
        for operation in (
            partial(service.save_session_global, item_id, "edit", conversation),
            partial(service.delete_session_global, item_id, conversation),
            partial(service.restore_session_global, item_id, conversation),
        ):
            with pytest.raises(ValueError):
                operation()
    for content in ("", "  \n", "x" * 4001):
        with pytest.raises(ValueError, match="1–4000"):
            service.save_session_global(item.id, content, "c")
    assert service.effective_global("c") == [item]
    assert service.list("c", "b") == [local]


def test_session_global_effective_size_limit_is_atomic(memory_stack):
    service, _ = memory_stack
    for _ in range(7):
        service.save("x" * 4000)
    item = service.save("small")
    other = service.save("small")
    service.save_session_global(item.id, "x" * 3995, "c")
    with pytest.raises(ValueError, match="记忆空间已满"):
        service.save_session_global(other.id, "too long", "c")
    assert service.effective_global("c")[-1] == other
    service.delete_session_global(item.id, "c")
    service.save_session_global(other.id, "now fits", "c")


def test_session_global_cascade_keeps_other_conversation_and_global(memory_stack):
    service, factory = memory_stack
    _second_conversation(factory)
    item = service.save("original")
    service.save_session_global(item.id, "custom", "c")
    service.delete_session_global(item.id, "second")
    with factory() as uow:
        doc = uow.memories.get("global-overrides:c")
        assert doc.conversation_id == "c" and doc.branch_id is None
        uow.conversations.delete("c")
        uow.commit()
    with factory() as uow:
        assert uow.memories.get("global-overrides:c") is None
    assert service.effective_global("second") == []
    assert service.list() == [item]


def test_session_global_encryption_and_reopen(tmp_path, vault_key):
    path = tmp_path / "overrides.db"
    factory = sqlite_uow_factory(path, vault_key)
    config = ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    service = MemoryService(factory, config)
    _second_conversation(factory)
    first = service.save("global one")
    second = service.save("global two")
    service.save_session_global(first.id, "SESSION_OVERRIDE_SECRET", "second")
    service.delete_session_global(second.id, "second")
    factory.close()
    assert b"SESSION_OVERRIDE_SECRET" not in path.read_bytes()
    reopened = sqlite_uow_factory(path, vault_key)
    try:
        service = MemoryService(reopened, config)
        assert [i.content for i in service.effective_global("second")] == [
            "SESSION_OVERRIDE_SECRET"
        ]
        assert [e.state for e in service.list_session_global("second")] == ["modified", "deleted"]
        assert service.list() == [first, second]
    finally:
        reopened.close()
