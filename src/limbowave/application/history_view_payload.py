"""Process-safe history data; image decoding and Qt rendering stay outside this module."""

from dataclasses import dataclass

from limbowave.application.history_payload import HistoryEntry, history_payload
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.permission_service import PermissionService
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.permissions import Capability, PermissionPreset


@dataclass(frozen=True)
class HistoryViewPayload:
    branch_id: str
    messages: list[Message]
    entries: list[HistoryEntry]
    attachment_ids: dict[str, tuple[str, ...]]
    preset: PermissionPreset
    capabilities: set[Capability]
    branches: list[tuple[str, str, int]]


def load_history_view(
    factory: UnitOfWorkFactory, conversation_id: str, branch_id: str | None,
) -> HistoryViewPayload | None:
    history = HistoryService(factory)
    permissions = PermissionService(factory)
    if branch_id is None:
        opened = history.open_conversation(conversation_id)
        if opened is None:
            return None
        branch_id, messages = opened
    else:
        with factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None or branch.conversation_id != conversation_id:
                return None
        messages = history.branch_messages(branch_id)
    entries = history_payload(messages, uow_factory=factory, branch_id=branch_id)
    ids: dict[str, tuple[str, ...]] = {
        message.id: () for message in messages if message.role is MessageRole.USER
    }
    if ids:
        with factory() as uow:
            for intent in uow.snapshots.list_all_intents():
                if intent.message_ids and intent.message_ids[-1] in ids:
                    ids[intent.message_ids[-1]] = tuple(intent.attachment_ids)
    return HistoryViewPayload(
        branch_id, messages, entries, ids, permissions.get_preset(conversation_id),
        {grant.capability for grant in permissions.list_grants(conversation_id)},
        history.list_branches(conversation_id),
    )
