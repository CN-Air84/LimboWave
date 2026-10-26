"""内置预设使用普通加密记忆；初始化不得覆盖修改或复活已删除条目。"""

import json
from dataclasses import asdict

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService
from limbowave.domain.memory import MemoryDocument, MemoryItem, MemoryRunContext
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from tests.unit.test_memory_service import memory_stack as memory_stack


def test_builtin_preset_is_one_editable_global_item_and_installs_once(memory_stack):
    service, _ = memory_stack
    assert service.ensure_builtin_preset()
    preset, = service.list()
    assert preset.content.startswith("忠实的阅读者 · 内置预设\n")
    assert preset.source == "builtin"
    assert MemoryService.validate_content(preset.content) == preset.content
    assert not service.list("c", "b")
    assert not service.ensure_builtin_preset()
    assert service.list() == [preset]
    context = service.prompt_context(MemoryRunContext("run", "c", "b", "msg"), 1)
    assert context["global"] == [preset.content]


def test_install_preserves_existing_global_and_session_memories(memory_stack):
    service, _ = memory_stack
    original = service.save("已有全局偏好")
    local = service.save("已有分支记忆", "c", "b")
    assert service.ensure_builtin_preset()
    assert service.list()[0] == original
    assert len(service.list()) == 2
    assert service.list("c", "b") == [local]


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_user_changes_survive_reinitialization(memory_stack, tmp_path, action):
    service, factory = memory_stack
    service.ensure_builtin_preset()
    preset, = service.list()
    if action == "edit":
        service.save("我定制的阅读者", item_id=preset.id)
    else:
        service.delete(preset.id)
    expected = service.list()
    recreated = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json")),
    )
    assert not recreated.ensure_builtin_preset()
    assert recreated.list() == expected


def test_builtin_uses_existing_session_customization_and_prompt_refresh(memory_stack):
    service, _ = memory_stack
    service.ensure_builtin_preset()
    preset, = service.list()
    context = MemoryRunContext("run", "c", "b", "msg")
    before = service.prompt_context(context, 1)
    service.save_session_global(preset.id, "本会话更简短的阅读者", "c")
    assert service.prompt_context(context, 2)["global"] == ["本会话更简短的阅读者"]
    assert service.list() == [preset]
    assert before["global"] == [preset.content]
    service.delete_session_global(preset.id, "c")
    assert service.prompt_context(context, 3)["global"] == []
    service.restore_session_global(preset.id, "c")
    service.save("全局修改后的正文", item_id=preset.id)
    assert service.prompt_context(context, 4)["global"] == ["全局修改后的正文"]
    service.delete(preset.id)
    assert service.prompt_context(context, 5)["global"] == []


@pytest.mark.parametrize("limit", ["count", "characters"])
def test_full_global_scope_defers_without_losing_user_data(memory_stack, limit):
    service, factory = memory_stack
    contents = ["已有偏好"] * 100 if limit == "count" else ["字" * 4000] * 8
    originals = [MemoryItem(str(i), text, "user", "now") for i, text in enumerate(contents)]
    with factory() as uow:
        uow.memories.put(MemoryDocument(
            "global", json.dumps([asdict(item) for item in originals], ensure_ascii=False),
        ))
        uow.commit()
    assert not service.ensure_builtin_preset()
    assert service.list() == originals
    service.delete(originals[-1].id)
    assert service.ensure_builtin_preset()
    assert service.list()[:-1] == originals[:-1]
    assert not service.ensure_builtin_preset()


def test_preset_and_receipt_are_atomic(memory_stack, tmp_path):
    service, factory = memory_stack

    def fail_commit():
        raise RuntimeError("commit failed")

    class FailedCommit:
        def __enter__(self):
            self.uow = factory()
            self.uow.__enter__()
            self.uow.commit = fail_commit
            return self.uow

        def __exit__(self, *args):
            return self.uow.__exit__(*args)

    failing = MemoryService(
        FailedCommit, ConfigurationService(JsonConfigRepository(tmp_path / "config.json")),
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        failing.ensure_builtin_preset()
    assert not service.list()
    assert service.ensure_builtin_preset()
    assert len(service.list()) == 1


@pytest.mark.parametrize("action", ["keep", "edit", "delete"])
def test_sqlite_reopen_preserves_preset_choices_and_encryption(tmp_path, vault_key, action):
    path = tmp_path / "preset.db"
    config = ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    factory = sqlite_uow_factory(path, vault_key)
    try:
        service = MemoryService(factory, config)
        service.ensure_builtin_preset()
        preset, = service.list()
        if action == "edit":
            service.save("PRIVATE_READER_CUSTOMIZATION", item_id=preset.id)
        elif action == "delete":
            service.delete(preset.id)
        expected = service.list()
    finally:
        factory.close()
    data = path.read_bytes()
    assert preset.content.encode("utf-8") not in data
    assert b"PRIVATE_READER_CUSTOMIZATION" not in data
    reopened = sqlite_uow_factory(path, vault_key)
    try:
        service = MemoryService(reopened, config)
        assert not service.ensure_builtin_preset()
        assert service.list() == expected
    finally:
        reopened.close()


def test_existing_preset_identity_without_receipt_is_not_overwritten(memory_stack):
    from limbowave.domain.memory_presets import BUILTIN_READER_PRESET_ID

    service, factory = memory_stack
    existing = MemoryItem(BUILTIN_READER_PRESET_ID, "保留已有定制正文", "user", "original-time")
    with factory() as uow:
        uow.memories.put(MemoryDocument("global", json.dumps([asdict(existing)])))
        uow.commit()
    assert not service.ensure_builtin_preset()
    assert service.list() == [existing]
    service.delete(existing.id)
    assert not service.ensure_builtin_preset()
    assert service.list() == []


def test_concurrent_service_initialization_installs_only_once(memory_stack, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, factory = memory_stack
    barrier = Barrier(4)

    def initialize(_index):
        other = MemoryService(
            factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json")),
        )
        barrier.wait(timeout=5)
        return other.ensure_builtin_preset()

    with ThreadPoolExecutor(max_workers=4) as executor:
        installed = list(executor.map(initialize, range(4)))
    assert installed.count(True) == 1
    assert installed.count(False) == 3
    assert len(service.list()) == 1


async def test_unsupported_kernel_does_not_silently_drop_builtin_preset(memory_stack):
    from limbowave.application.kernel import AgentKernel
    from tests.unit.test_run_coordinator import FakeKernel

    class UnsupportedMemoryKernel(FakeKernel):
        set_memory_context = AgentKernel.set_memory_context

    service, _ = memory_stack
    service.ensure_builtin_preset()
    context = service.prompt_context(MemoryRunContext("run", "c", "b", "message"), 1)
    kernel = UnsupportedMemoryKernel()
    with pytest.raises(NotImplementedError, match="不支持记忆上下文"):
        await kernel.set_memory_context(context)
    assert kernel.sent == []
