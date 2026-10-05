"""Snapshot compression decisions when creating a branch; never share mutable versions."""

from dataclasses import replace
from uuid import uuid4

from limbowave.application.branch_path import branch_messages
from limbowave.application.repositories import UnitOfWork
from limbowave.domain.compaction import CompressionStatus
from limbowave.domain.compression_inheritance import compression_survives_fork
from limbowave.domain.conversation import Branch


def inherit_compressions(uow: UnitOfWork, branch: Branch) -> bool:
    """Copy eligible accepted history/activation in the caller's branch transaction.

    Return whether the source has compression history, even if the fork cuts inside
    it: in that case its stale runtime overlays still need to be removed.
    """
    if branch.parent_branch_id is None:
        return False
    versions = [
        v
        for v in uow.compressions.list_for_branch(branch.parent_branch_id)
        if v.status is CompressionStatus.ACCEPTED
    ]
    active = uow.compressions.get_active(branch.parent_branch_id)
    prefix = tuple(m.id for m in branch_messages(uow, branch.id))
    for version in versions:
        if not compression_survives_fork(
            version.input_message_ids,
            prefix,
            version_created_at=version.created_at,
            branch_created_at=branch.created_at,
        ):
            continue
        inherited = replace(version, id=f"cmp_{uuid4().hex[:16]}", branch_id=branch.id)
        uow.compressions.add(inherited)
        if active is not None and active.id == version.id:
            uow.compressions.set_active(branch.id, inherited.id)
    return bool(versions)
