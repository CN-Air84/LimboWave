"""Coordinate manual compression and automatic previews on the UI event loop.

The caller still selects thresholds, validates the current session, and performs
compression. This guard only admits jobs: claim synchronously before the first
await, and always release the returned ticket in a finally block. It never changes
conversation history, usage estimates, or persisted compression versions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, eq=False)
class CompressionAttempt:
    """Identity ticket for a single admitted job; pass it back to ``finish``."""

    branch_id: str


class CompressionTrigger:
    """One compression job at a time and at most one auto preview per branch.

    Manual generation counts as having handled that branch's preview, even on
    failure or rejection. A subsequent reply must not silently start it again;
    the user can always explicitly retry once the running job has finished.

    This follows the existing per-branch, application-lifetime preview policy.
    Use ``mark_handled`` when restoring an already accepted compression version
    so reopening that branch does not turn it into an unhandled preview.
    """

    def __init__(self) -> None:
        self._handled_branches: set[str] = set()
        self._active: CompressionAttempt | None = None

    @property
    def busy(self) -> bool:
        return self._active is not None

    def try_begin(
        self, branch_id: str, *, automatic: bool = False,
    ) -> CompressionAttempt | None:
        """Atomically admit a job, or decline without consuming its opportunity.

        All callers must share one guard and invoke it on the same event loop.
        There is no await between checking and claiming, so simultaneous usage
        refresh callbacks cannot both schedule a compression job.
        """
        if self._active is not None:
            return None
        if automatic and branch_id in self._handled_branches:
            return None
        attempt = CompressionAttempt(branch_id)
        self._handled_branches.add(branch_id)
        self._active = attempt
        return attempt

    def finish(self, attempt: CompressionAttempt) -> None:
        """Release only this job; delayed/duplicate cleanup cannot unlock another."""
        if self._active is attempt:
            self._active = None

    def mark_handled(self, branch_id: str) -> None:
        """Record restored/applied compression without touching the execution lock."""
        self._handled_branches.add(branch_id)
