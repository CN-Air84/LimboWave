"""Compression jobs must not restart on the next reply or overlap refresh callbacks."""

from __future__ import annotations

import asyncio

import pytest

from limbowave.application.services.compression_trigger import CompressionTrigger


def test_manual_compression_suppresses_next_reply_auto_preview() -> None:
    trigger = CompressionTrigger()
    attempt = trigger.try_begin("branch-1")
    assert attempt is not None
    trigger.finish(attempt)

    for _ in range(3):
        assert trigger.try_begin("branch-1", automatic=True) is None
    assert not trigger.busy


def test_automatic_preview_only_runs_once_per_branch() -> None:
    trigger = CompressionTrigger()
    attempt = trigger.try_begin("branch-1", automatic=True)
    assert attempt is not None
    assert trigger.busy
    assert trigger.try_begin("branch-1", automatic=True) is None
    trigger.finish(attempt)
    assert trigger.try_begin("branch-1", automatic=True) is None


def test_explicit_manual_retry_is_still_allowed() -> None:
    trigger = CompressionTrigger()
    first = trigger.try_begin("branch-1", automatic=True)
    assert first is not None
    trigger.finish(first)
    retry = trigger.try_begin("branch-1")
    assert retry is not None
    assert trigger.busy
    trigger.finish(retry)
    assert not trigger.busy


@pytest.mark.parametrize("automatic", [False, True])
def test_inflight_job_blocks_manual_and_automatic_reentry(automatic: bool) -> None:
    trigger = CompressionTrigger()
    active = trigger.try_begin("branch-1")
    assert active is not None
    assert trigger.try_begin("branch-1", automatic=automatic) is None
    assert trigger.try_begin("branch-2", automatic=automatic) is None
    trigger.finish(active)

    # A declined request must not use branch 2's one automatic opportunity.
    other = trigger.try_begin("branch-2", automatic=True)
    assert other is not None
    trigger.finish(other)


def test_stale_completion_cannot_release_a_new_attempt_on_same_branch() -> None:
    trigger = CompressionTrigger()
    first = trigger.try_begin("branch-1")
    assert first is not None
    trigger.finish(first)
    second = trigger.try_begin("branch-1")
    assert second is not None

    trigger.finish(first)
    assert trigger.busy
    assert trigger.try_begin("branch-2") is None
    trigger.finish(second)
    assert not trigger.busy
    trigger.finish(second)  # Repeated cleanup is harmless.
    assert not trigger.busy


def test_ticket_from_other_trigger_cannot_release_active_attempt() -> None:
    trigger, unrelated = CompressionTrigger(), CompressionTrigger()
    active = trigger.try_begin("branch-1")
    foreign = unrelated.try_begin("branch-1")
    assert active is not None and foreign is not None
    trigger.finish(foreign)
    assert trigger.busy
    trigger.finish(active)
    unrelated.finish(foreign)


def test_restored_accepted_version_suppresses_automatic_not_manual_jobs() -> None:
    trigger = CompressionTrigger()
    trigger.mark_handled("branch-1")
    trigger.mark_handled("branch-1")
    assert not trigger.busy
    assert trigger.try_begin("branch-1", automatic=True) is None
    manual = trigger.try_begin("branch-1")
    assert manual is not None
    trigger.finish(manual)
    fresh = trigger.try_begin("branch-2", automatic=True)
    assert fresh is not None
    trigger.finish(fresh)


@pytest.mark.parametrize("automatic", [False, True])
def test_failure_cleanup_requires_explicit_retry(automatic: bool) -> None:
    trigger = CompressionTrigger()
    attempt = trigger.try_begin("branch-1", automatic=automatic)
    assert attempt is not None
    with pytest.raises(RuntimeError, match="generation failed"):
        try:
            raise RuntimeError("generation failed")
        finally:
            trigger.finish(attempt)
    assert not trigger.busy
    assert trigger.try_begin("branch-1", automatic=True) is None
    retry = trigger.try_begin("branch-1")
    assert retry is not None
    trigger.finish(retry)


async def test_concurrent_usage_refreshes_only_start_one_generation() -> None:
    trigger = CompressionTrigger()
    started: list[str] = []

    async def refresh() -> None:
        attempt = trigger.try_begin("branch-1", automatic=True)
        if attempt is None:
            return
        try:
            started.append("generation")
            await asyncio.sleep(0)
        finally:
            trigger.finish(attempt)

    await asyncio.gather(*(refresh() for _ in range(20)))
    assert started == ["generation"]
    assert not trigger.busy


async def test_cancellation_releases_execution_lock_without_auto_retry() -> None:
    trigger = CompressionTrigger()
    started = asyncio.Event()

    async def generate() -> None:
        attempt = trigger.try_begin("branch-1")
        assert attempt is not None
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            trigger.finish(attempt)

    task = asyncio.create_task(generate())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not trigger.busy
    assert trigger.try_begin("branch-1", automatic=True) is None
    other = trigger.try_begin("branch-2", automatic=True)
    assert other is not None
    trigger.finish(other)


def test_marking_restored_branch_does_not_release_another_running_job() -> None:
    trigger = CompressionTrigger()
    active = trigger.try_begin("branch-1")
    assert active is not None
    trigger.mark_handled("branch-2")
    assert trigger.busy
    assert trigger.try_begin("branch-3", automatic=True) is None
    trigger.finish(active)
    assert trigger.try_begin("branch-2", automatic=True) is None
    fresh = trigger.try_begin("branch-3", automatic=True)
    assert fresh is not None
    trigger.finish(fresh)
