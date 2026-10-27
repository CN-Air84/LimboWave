"""PRIV-02: inspect actual allowed payloads, not merely saved switches."""

from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.services.chat_history_service import ChatHistoryService
from limbowave.application.services.compression_service import CompressionService
from limbowave.application.services.privacy_service import (
    PRIVACY_KEY,
    PrivacyService,
    allowed_messages,
)
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.memory import MemoryDocument, MemoryRunContext
from tests.unit.test_memory_service import NOW
from tests.unit.test_memory_service import memory_stack as memory_stack
from tests.unit.test_run_coordinator import FakeKernel


def seed(factory, text, *, mid="m1", role=MessageRole.USER, created=NOW):
    message = Message(mid, "c", "b", role, text, created)
    with factory() as uow:
        uow.messages.add(message)
        uow.commit()
    return message


def test_rules_survive_recreation_and_do_not_delete_history(memory_stack):
    service, factory = memory_stack
    original = seed(factory, "OldProject — a private event")
    rule = service.privacy.add("那次离职", ["ＯＬＤＰＲＯＪＥＣＴ"], [original.id])
    restored = PrivacyService(factory)
    policy = restored.policy()
    assert policy.rules[0].id == rule.id
    assert policy.blocks("old project")
    assert policy.blocks("unrelated words", original.id)
    assert not policy.blocks("cooking")
    assert not ChatHistoryService(factory).query()["results"]
    with factory() as uow:
        assert uow.messages.get(original.id).content == original.content
    restored.remove(rule.id)
    assert ChatHistoryService(factory).query()["results"]


def test_old_derived_data_and_scratchpad_are_quarantined(memory_stack, monkeypatch):
    service, factory = memory_stack
    old = service.save("opaque paraphrase without keywords", "c", "b")
    service.save("private global detail")
    service.write_scratchpad("c", "b", "another paraphrase", 0)
    service.privacy.add("unrelated topic")
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(seconds=1)

    monkeypatch.setattr("limbowave.application.services.memory_service.datetime", Later)
    fresh = service.save("new safe preference", "c", "b")
    ctx = service.prompt_context(MemoryRunContext("r", "c", "b", "u"), 1)
    assert ctx["global"] == []
    assert ctx["session"] == [fresh.content]
    assert ctx["scratchpad"]["content"] == ""
    assert service.search("c", "b", old.id)["results"] == []
    assert old in service.list("c", "b")  # User-facing management stays complete.
    with pytest.raises(ValueError, match="话题禁区"):
        CompressionService(factory).build_prompt("b")


def test_message_filter_preserves_safe_user_text_not_old_paraphrases(memory_stack):
    service, factory = memory_stack
    safe = seed(factory, "I enjoy tea")
    excluded = seed(factory, "private topic", mid="m2")
    assistant = seed(factory, "opaque paraphrase", mid="m3", role=MessageRole.ASSISTANT)
    service.privacy.add("private topic")
    assert allowed_messages(service.privacy.policy(), [safe, excluded, assistant]) == [safe]


async def test_fresh_conversation_is_durable_and_history_is_preserved(memory_stack):
    service, factory = memory_stack
    seed(factory, "do not bring this past")
    service.save("GLOBAL_PERSONAL_PREFERENCE")
    kernel = FakeKernel()
    coordinator = RunCoordinator(kernel, factory, context=lambda: RunContext("m", "e", "test"))
    coordinator.memory_service = service
    assert await coordinator.new_session(fresh_start=True)
    cid, bid = coordinator.conversation_id, coordinator.branch_id
    assert cid != "c" and service.privacy.policy(cid).fresh_start
    assert ChatHistoryService(factory).query(requester_conversation_id=cid)["results"] == []
    ctx = coordinator._memory_prompt(MemoryRunContext("r", cid, bid, "u"))
    assert ctx["global"] == [] and ctx["privacy"]["history"] == ""
    with factory() as uow:
        assert uow.messages.get("m1") is not None
    assert await coordinator.send("new beginning")
    assert kernel.memory_context["privacy"]["history"] == ""
    await coordinator.mark_interrupted()
    await coordinator.wait_idle()


def test_coordinator_rebuilds_only_allowed_plaintext(memory_stack):
    service, factory = memory_stack
    seed(factory, "SAFE_ORIGINAL")
    seed(factory, "EXCLUDED_ORIGINAL", mid="m2")
    seed(factory, "OPAQUE_ASSISTANT", mid="m3", role=MessageRole.ASSISTANT)
    service.privacy.add("EXCLUDED_ORIGINAL")
    seed(factory, "current", mid="current", created=datetime.now(UTC) + timedelta(seconds=1))
    coordinator = RunCoordinator(None, factory, context=lambda: RunContext("m", "e", "test"))
    # Even a composition without MemoryService must install the privacy boundary.
    context = coordinator._memory_prompt(MemoryRunContext("r", "c", "b", "current"))
    history = context["privacy"]["history"]
    assert "SAFE_ORIGINAL" in history
    assert "EXCLUDED_ORIGINAL" not in history and "OPAQUE_ASSISTANT" not in history
    assert "current" not in history


def test_busy_and_malformed_policy_fail_closed(memory_stack):
    service, factory = memory_stack
    service.privacy.is_busy = lambda: True
    with pytest.raises(ValueError, match="停止"):
        service.privacy.add("private")
    with factory() as uow:
        uow.memories.put(MemoryDocument(PRIVACY_KEY, '{"version": 999}'))
        uow.commit()
    with pytest.raises(ValueError):
        service.prompt_context(MemoryRunContext("r", "c", "b", "u"), 1)
    with pytest.raises(ValueError):
        ChatHistoryService(factory).query()


def test_backup_restore_preserves_encrypted_privacy_rules(tmp_path):
    from limbowave.application.services.backup_service import BackupService
    from limbowave.application.services.restore_service import RestoreService
    from limbowave.domain.conversation import Branch, Conversation
    from limbowave.infrastructure.crypto.vault import Vault
    from limbowave.infrastructure.database.migrations import CURRENT_VERSION
    from tests.conftest import TEST_KDF, make_vault
    from tests.unit.test_memory_preset_backup import _memories

    source = tmp_path / "source"
    source.mkdir()
    key = make_vault(source / "vault.json")
    service, factory = _memories(source, key)
    archive = tmp_path / "privacy.lwb"
    try:
        with factory() as uow:
            uow.conversations.add(Conversation("c", "chat", NOW))
            uow.branches.add(Branch("b", "c", NOW))
            PrivacyService.mark_fresh(uow, "c")
            uow.commit()
        rule = service.privacy.add("ENCRYPTED_PRIVATE_TOPIC", ["ALIAS_CANARY"])
        backup = BackupService(source, app_version="test", schema_version=CURRENT_VERSION,
                               kdf_params=TEST_KDF, vault_key=key)
        backup.create(archive, "backup password")
    finally:
        factory.close()
    assert b"ENCRYPTED_PRIVATE_TOPIC" not in archive.read_bytes()
    assert b"ALIAS_CANARY" not in (source / "limbowave.db").read_bytes()
    destination = tmp_path / "restored"
    destination.mkdir()
    RestoreService(destination, backup).prepare(archive, "backup password").execute()
    vault = Vault(destination / "vault.json")
    restored, factory = _memories(destination, vault.unlock("backup password"))
    try:
        assert restored.privacy.policy("c").fresh_start
        assert restored.privacy.policy().rules[0] == rule
    finally:
        factory.close()
        vault.lock()


def test_tool_result_boundary_is_unicode_consistent(memory_stack, tmp_path):
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway
    from limbowave.domain.permissions import Capability

    service, factory = memory_stack
    service.privacy.add("STRASSE")
    (tmp_path / "Straße.txt").write_text("details", encoding="utf-8")
    permissions = PermissionService(factory)
    permissions.grant("c", Capability.FILE_READ, allowed_paths=(str(tmp_path),))
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    result = gateway.invoke("list_directory", {"path": "."}, "c")
    assert not result.ok and result.data == {}
    assert "Straße" not in str(result)


def test_isolated_model_request_blocks_rule_changes(memory_stack):
    from limbowave.application.services.privacy_service import privacy_model_request

    service, _factory = memory_stack
    with privacy_model_request(), pytest.raises(ValueError, match="停止"):
        service.privacy.add("topic")
    assert service.privacy.add("topic")


def test_explicit_source_copies_and_current_turn_memory_are_blocked(memory_stack):
    service, factory = memory_stack
    source = seed(factory, "content with no topic label")
    service.privacy.add("unrelated label", source_ids=[source.id])
    copy = seed(factory, source.content, mid="new-copy-id")
    policy = service.privacy.policy()
    assert policy.blocks(copy.content, copy.id)
    assert not service.privacy_allows_model_memory(MemoryRunContext("r", "c", "b", copy.id))
    assert not allowed_messages(policy, [source, copy])


def test_excluded_historical_input_cannot_be_replayed_by_retry(memory_stack):
    service, factory = memory_stack
    source = seed(factory, "excluded input")
    service.privacy.add("excluded")
    coordinator = RunCoordinator(None, factory, context=lambda: RunContext("m", "e", "test"))
    coordinator._active_conversation_id = "c"
    coordinator._active_branch_id = "b"
    with pytest.raises(ValueError, match="话题禁区"):
        coordinator._prepare_retry(source.id)
