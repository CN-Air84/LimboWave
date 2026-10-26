"""Global originals may be updated only after explicit scope-bound consent."""

from dataclasses import replace

import pytest

from limbowave.domain.memory import MemoryRunContext
from tests.unit.test_memory_service import memory_stack as memory_stack


def test_global_update_requires_consent_and_preserves_overrides(memory_stack):
    service, _ = memory_stack
    original = service.save("global original")
    service.save_session_global(original.id, "conversation override", "c")
    context = MemoryRunContext("r", "c", "b", "msg")
    change = service.prepare_mutation(context, "update_memory", original.id, "global updated")
    assert change.scope == "global" and change.original == original
    with pytest.raises(ValueError, match="授权"):
        service.mutate_from_model(context, "call", change, lambda _: True)
    service.approve_mutation(context, "call", change)
    result = service.mutate_from_model(context, "call", change, lambda _: True)
    updated, = service.list()
    assert updated.id == original.id and updated.content == "global updated"
    assert updated.created_at == original.created_at and updated.source == original.source
    assert updated.updated_at is not None
    assert result["scope"] == "global"
    assert service.effective_global("c")[0].content == "conversation override"
    assert not service.list("c", "b")
    assert service.mutate_from_model(context, "call", change, lambda _: True) == result


@pytest.mark.parametrize("failure", ["scope", "stale", "hidden", "cancelled", "quota"])
def test_global_update_rejects_invalidated_approval(memory_stack, failure):
    service, _ = memory_stack
    original = service.save("global original")
    context = MemoryRunContext("r", "c", "b", "msg")
    change = service.prepare_mutation(context, "update_memory", original.id, "x" * 4000)
    service.approve_mutation(context, "call", change)
    if failure == "scope":
        change = replace(change, scope="session")
    elif failure == "stale":
        service.save("newer user edit", item_id=original.id)
    elif failure == "hidden":
        service.delete_session_global(original.id, "c")
    elif failure == "quota":
        for _ in range(7):
            service.save("other" * 800)
        service.save("padding")  # Filling the global scope makes the proposed update overflow.
    before = service.list()
    with pytest.raises(ValueError):
        service.mutate_from_model(context, "call", change, lambda _: failure != "cancelled")
    assert service.list() == before
    assert not service.list("c", "b")


def test_hidden_global_and_global_delete_remain_unavailable(memory_stack):
    service, _ = memory_stack
    original = service.save("global original")
    context = MemoryRunContext("r", "c", "b", "msg")
    with pytest.raises(ValueError):
        service.prepare_mutation(context, "delete_session_memory", original.id)
    service.delete_session_global(original.id, "c")
    with pytest.raises(ValueError):
        service.prepare_mutation(context, "update_memory", original.id, "new")


def test_old_session_receipt_defaults_to_session_scope(memory_stack):
    import json

    from limbowave.domain.memory import MemoryDocument

    service, factory = memory_stack
    original = service.save("original", "c", "b")
    context = MemoryRunContext("r", "c", "b", "msg")
    change = service.prepare_mutation(context, "update_memory", original.id, "updated")
    service.approve_mutation(context, "call", change)
    result = service.mutate_from_model(context, "call", change, lambda _: True)
    with factory() as uow:
        document = uow.memories.get("receipt:model:r:call")
        payload = json.loads(document.payload)
        payload["change"].pop("scope")
        uow.memories.put(MemoryDocument(document.id, json.dumps(payload), "c", "b"))
        uow.commit()
    restored = service.mutation_for_call(context, "call", "update_memory", original.id, "updated")
    assert restored.scope == "session"
    assert service.mutate_from_model(context, "call", restored, lambda _: True) == result
