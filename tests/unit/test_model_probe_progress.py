"""Request-based progress never advances on headers, RPM waits or skipped requests."""
from __future__ import annotations

import json
from threading import Event

import httpx
import pytest

from limbowave.application.services import model_probe
from limbowave.application.services.endpoint_rate_limiter import (
    EndpointRateLimiter,
    RateLimitCancelled,
)
from limbowave.domain.providers import EndpointConfig, ProviderProtocol


def endpoint(api=ProviderProtocol.OPENAI_COMPLETIONS):
    return EndpointConfig(
        id="progress", name="Progress", base_url="https://example.com/v1", api=api,
    )


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, payload, progress, completed):
        self.payload, self.progress, self.completed = payload, progress, completed
        self.closed = False

    def __iter__(self):
        assert self.progress[-1].completed == self.completed
        yield b"data: " + json.dumps(self.payload).encode() + b"\n\ndata: [DONE]\n\n"
        assert self.progress[-1].completed == self.completed

    def close(self):
        assert self.progress[-1].completed == self.completed
        self.closed = True


@pytest.mark.parametrize("api", list(ProviderProtocol))
@pytest.mark.parametrize("tool_turns", [1, 2, 3])
def test_counts_each_closed_request_and_removes_unused_turns(monkeypatch, api, tool_turns):
    progress, streams = [], []
    original = httpx.Client
    thinking_count = 6 if api in (
        ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES,
    ) else 1
    total = 1 + thinking_count + 3
    requests = 1 + thinking_count + tool_turns

    def handler(request):
        stream = TrackingStream({"type": "content", "text": "ok"}, progress, len(streams))
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(model_probe.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    # Keep the real stream path; vary only the provider's parsed tool-turn answer.
    turns = iter([
        model_probe._ToolTurn((model_probe._ToolCall("1", "read_file"),), "", ())
        for _ in range(tool_turns - 1)
    ] + [model_probe._ToolTurn((), "", ())])
    for name in ("_completions_turn", "_responses_turn", "_anthropic_turn", "_google_turn"):
        monkeypatch.setattr(model_probe, name, lambda events: next(turns))
    clock = iter(range(0, 1000, 20))
    result = model_probe.probe_model_capabilities(
        endpoint(api), "m", None, on_progress=progress.append,
        limiter=EndpointRateLimiter(clock=lambda: next(clock)),
    )
    assert result.alive
    assert len(streams) == requests
    assert all(stream.closed for stream in streams)
    assert progress[:requests + 1] == [
        model_probe.ModelProbeProgress(i, total) for i in range(requests + 1)
    ]
    assert progress[-1] == model_probe.ModelProbeProgress(requests, requests)


@pytest.mark.parametrize("failure", ["http", "timeout", "empty"])
def test_failed_request_counts_once_and_unexecuted_capabilities_are_not_counted(
    monkeypatch, failure,
):
    original = httpx.Client
    progress, requests = [], []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(401 if failure == "http" else 200, text="")

    monkeypatch.setattr(model_probe.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    result = model_probe.probe_model_capabilities(
        endpoint(), "m", None, on_progress=progress.append, limiter=EndpointRateLimiter(),
    )
    assert not result.alive
    assert len(requests) == 1
    assert progress == [model_probe.ModelProbeProgress(0, 10),
                        model_probe.ModelProbeProgress(1, 10),
                        model_probe.ModelProbeProgress(1, 1)]


def test_cancelled_rpm_wait_does_not_count_as_completed_request():
    cancelled = Event()
    cancelled.set()
    progress = []
    with pytest.raises(RateLimitCancelled):
        model_probe.probe_model_capabilities(
            endpoint(), "m", None, cancelled=cancelled,
            on_progress=progress.append, limiter=EndpointRateLimiter(),
        )
    assert progress == [model_probe.ModelProbeProgress(0, 10)]


def test_missing_credential_has_no_completed_requests():
    progress = []
    result = model_probe.probe_model_capabilities(
        endpoint().model_copy(update={"credential_ref": "missing"}), "m", None,
        on_progress=progress.append,
    )
    assert not result.alive
    assert not any(p.completed for p in progress)


def test_rpm_wait_keeps_progress_unchanged_until_request_finishes(monkeypatch):
    original = httpx.Client
    now = 0.0
    progress, waits = [], []
    limiter = EndpointRateLimiter(clock=lambda: now)
    assert limiter.try_acquire(endpoint().id) == 0

    def wait(seconds):
        nonlocal now
        assert progress == [model_probe.ModelProbeProgress(0, 10)]
        waits.append(seconds)
        now += seconds

    monkeypatch.setattr(limiter._stopped, "wait", wait)
    monkeypatch.setattr(model_probe.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kw,
    ))
    model_probe.probe_model_capabilities(
        endpoint(), "m", None, on_progress=progress.append, limiter=limiter,
    )
    assert sum(waits) == pytest.approx(12)
    assert progress[-1] == model_probe.ModelProbeProgress(1, 1)
