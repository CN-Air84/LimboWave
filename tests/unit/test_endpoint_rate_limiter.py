from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from limbowave.application.services.endpoint_rate_limiter import (
    EndpointRateLimiter,
    RateLimitCancelled,
)
from limbowave.domain.providers import EndpointConfig, ProviderProtocol


def endpoint(rpm: int = 5) -> EndpointConfig:
    return EndpointConfig(id="a", name="A", base_url="https://example.com",
                          api=ProviderProtocol.OPENAI_COMPLETIONS, rpm=rpm)


def test_smooth_pacing_and_independent_sites() -> None:
    now = 100.0
    limiter = EndpointRateLimiter(clock=lambda: now)
    assert limiter.try_acquire("a") == 0
    assert limiter.try_acquire("a") == 12
    assert limiter.try_acquire("b") == 0
    now += 11
    assert limiter.try_acquire("a") == 1
    now += 1
    assert limiter.try_acquire("a") == 0
    # 空闲后也不累积突发额度。
    now += 300
    assert limiter.try_acquire("a") == 0
    assert limiter.try_acquire("a") == 12


def test_concurrent_callers_cannot_burst() -> None:
    limiter = EndpointRateLimiter(clock=lambda: 0)
    with ThreadPoolExecutor(max_workers=12) as executor:
        delays = list(executor.map(lambda _: limiter.try_acquire("a"), range(30)))
    assert delays.count(0) == 1
    assert delays.count(12) == 29


def test_saved_rpm_overrides_stale_probe_without_resetting_budget() -> None:
    limiter = EndpointRateLimiter(clock=lambda: 0)
    limiter.configure([endpoint(5)])
    assert limiter.try_acquire("a") == 0
    limiter.configure([endpoint(10)])
    assert limiter.try_acquire("a", rpm=5) == 6
    limiter.configure([endpoint(1)])
    assert limiter.try_acquire("a", rpm=5) == 60


def test_cancelled_probe_does_not_reserve_future_slot() -> None:
    limiter = EndpointRateLimiter(clock=lambda: 0)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(RateLimitCancelled):
        limiter.wait(endpoint(), cancelled=cancelled)
    assert limiter.try_acquire("a") == 0


async def test_stop_interrupts_wait_and_does_not_block_event_loop() -> None:
    limiter = EndpointRateLimiter()
    limiter.wait(endpoint())
    waiting = asyncio.create_task(asyncio.to_thread(limiter.wait, endpoint()))
    await asyncio.sleep(0.03)
    assert not waiting.done()
    limiter.stop()
    with pytest.raises(RateLimitCancelled):
        await asyncio.wait_for(waiting, timeout=1)
    with pytest.raises(RateLimitCancelled):
        limiter.try_acquire("other")
