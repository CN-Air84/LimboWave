"""Eligibility for copying a compression into a fork, without inspecting its summary."""

from collections.abc import Sequence
from datetime import datetime


def compression_survives_fork(
    input_message_ids: Sequence[str],
    prefix_ids: Sequence[str],
    *,
    version_created_at: datetime,
    branch_created_at: datetime,
) -> bool:
    # A later preview must never leak future context into an older branch.
    return (
        version_created_at <= branch_created_at
        and bool(input_message_ids)
        and tuple(prefix_ids[: len(input_message_ids)]) == tuple(input_message_ids)
    )
