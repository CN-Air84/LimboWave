"""Offline invocation isolation and cleanup for the public-web privacy hook."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from limbowave.application.services.network_privacy import (
    check_network_destination,
    guard_network_request,
)
from limbowave.domain.network import NetworkSettings, WebAccessError
from limbowave.infrastructure.network_backend import create_network_tools
from limbowave.infrastructure.web_access import PublicHTTPClient


def test_no_scope_is_noop_and_guard_checks_current_scope():
    seen = []
    check_network_destination("https://example.com/outside")
    with guard_network_request(seen.append):
        check_network_destination("https://example.com/inside")
    check_network_destination("https://example.com/outside-again")
    assert seen == ["https://example.com/inside"]


def test_nested_scope_adds_checks_and_restores_outer():
    outer, inner = [], []
    with guard_network_request(outer.append):
        check_network_destination("first")
        with guard_network_request(inner.append):
            check_network_destination("second")
        check_network_destination("third")
    check_network_destination("outside")
    assert outer == ["first", "second", "third"] and inner == ["second"]


def test_nested_allow_cannot_override_outer_denial():
    inner = Mock()

    def deny(url):
        raise RuntimeError("SENSITIVE-DETAIL")

    with (
        guard_network_request(deny),
        guard_network_request(inner),
        pytest.raises(WebAccessError) as exc,
    ):
        check_network_destination("https://secret.example/")
    assert exc.value.code == "privacy_denied"
    assert "secret" not in str(exc.value) and "SENSITIVE" not in str(exc.value)
    assert exc.value.data == {} and exc.value.__suppress_context__
    inner.assert_not_called()
    check_network_destination("https://secret.example/")


def test_failing_guard_and_failing_body_restore_prior_context():
    outer = []

    def broken(url):
        raise OSError("SECRET")

    with guard_network_request(outer.append):
        with pytest.raises(WebAccessError), guard_network_request(broken):
            check_network_destination("first")
        with pytest.raises(RuntimeError), guard_network_request(broken):
            raise RuntimeError("body failure")
        check_network_destination("second")
    assert outer == ["first", "second"]
    check_network_destination("outside")


def test_invalid_guard_fails_closed_without_installing_state():
    with pytest.raises(WebAccessError) as exc, guard_network_request(None):
        pytest.fail("Invalid guard must not yield")
    assert exc.value.code == "privacy_denied"
    check_network_destination("outside")


@pytest.mark.parametrize("returned", [True, False, "ALLOW"])
def test_boolean_predicate_is_not_silently_treated_as_allowed(returned):
    with guard_network_request(lambda url: returned), pytest.raises(WebAccessError) as exc:
        check_network_destination("url")
    assert exc.value.code == "privacy_denied"


async def test_concurrent_async_invocations_do_not_share_checks():
    ready = [asyncio.Event(), asyncio.Event()]
    seen = [[], []]

    async def invoke(index):
        with guard_network_request(seen[index].append):
            ready[index].set()
            await ready[1 - index].wait()
            check_network_destination(f"conversation-{index}")
            await asyncio.sleep(0)
            check_network_destination(f"conversation-{index}-again")
        check_network_destination("outside")

    await asyncio.gather(invoke(0), invoke(1))
    assert seen == [
        ["conversation-0", "conversation-0-again"],
        ["conversation-1", "conversation-1-again"],
    ]


async def test_async_cancellation_resets_guard_in_cancelled_task():
    ready = asyncio.Event()
    seen = []

    async def invoke():
        try:
            with guard_network_request(seen.append):
                check_network_destination("inside")
                ready.set()
                await asyncio.Event().wait()
        finally:
            check_network_destination("cleanup-outside")

    task = asyncio.create_task(invoke())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    check_network_destination("parent")
    assert seen == ["inside"]


def test_concurrent_thread_invocations_isolated_and_worker_reuse_cleans_up():
    barrier = Barrier(2)
    seen = [[], []]

    def invoke(index):
        with guard_network_request(seen[index].append):
            barrier.wait(timeout=3)
            check_network_destination(f"thread-{index}")
        check_network_destination("outside")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, index) for index in range(2)]
        for future in futures:
            future.result(timeout=5)
        executor.submit(check_network_destination, "reused-worker").result(timeout=5)
    assert seen == [["thread-0"], ["thread-1"]]


async def test_to_thread_copies_guard_and_does_not_leak_to_next_worker_use():
    seen = []
    with guard_network_request(seen.append):
        await asyncio.to_thread(check_network_destination, "inside-worker")
    await asyncio.to_thread(check_network_destination, "outside-worker")
    assert seen == ["inside-worker"]


def test_explicit_context_copy_is_supported_without_global_mutation():
    seen = []
    with guard_network_request(seen.append):
        copied = copy_context()
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(copied.run, check_network_destination, "copied").result(timeout=5)
        executor.submit(check_network_destination, "not-copied").result(timeout=5)
    assert seen == ["copied"]


def test_desktop_backend_consults_invocation_guard_at_call_time(monkeypatch):
    resolver = Mock(side_effect=AssertionError("must not resolve"))
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(PublicHTTPClient, "_resolve", resolver)
    monkeypatch.setattr(PublicHTTPClient, "_connection", connect)
    settings = Mock()
    settings.load.return_value = SimpleNamespace(network=NetworkSettings())
    backend = create_network_tools(settings, None)  # Constructed outside any invocation scope.

    def deny(url):
        raise RuntimeError("private topic")

    with guard_network_request(deny), pytest.raises(WebAccessError) as exc:
        backend.read_url("https://example.com/article")
    assert exc.value.code == "privacy_denied"
    resolver.assert_not_called()
    connect.assert_not_called()
    assert backend.fetch_configured and not backend.search_configured
