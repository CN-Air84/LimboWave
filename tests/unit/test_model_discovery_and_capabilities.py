"""站点模型发现与流式/思考/工具能力探测。"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx
import pytest

from limbowave.application.services import model_probe
from limbowave.domain.providers import EndpointConfig, ProviderProtocol


def _endpoint(protocol: ProviderProtocol = ProviderProtocol.OPENAI_COMPLETIONS) -> EndpointConfig:
    return EndpointConfig(
        id="relay",
        name="Relay",
        base_url="https://example.com/v1",
        api=protocol,
        credential_ref="key",
    )


def test_discover_models_reads_openai_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/models"
            assert request.headers["authorization"] == "Bearer private"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "qwen-3.8-max"},
                        {"id": "glm-high", "name": "GLM High"},
                    ]
                },
            )

        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.discover_models(_endpoint(), "private")
    assert [model.id for model in result.models] == ["qwen-3.8-max", "glm-high"]
    assert result.models[1].name == "GLM High"


def _event(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n")


def _read_file_call() -> httpx.Response:
    return _event({"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }]}}]})


def _tool_turn(body: dict[str, Any]) -> httpx.Response:
    """扮演会用工具的模型：先调用 read_file，拿到工具结果后原样复述。"""
    last = body["messages"][-1]
    if last["role"] == "tool":
        return _event({"choices": [{"delta": {"content": last["content"]}}]})
    return _read_file_call()


def test_suffix_does_not_skip_probes_or_infer_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    levels: list[str] = []

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "tools" in body:
                return _tool_turn(body)
            level = body.get("reasoning_effort")
            if level:
                levels.append(level)
            return _event({
                "model": "provider-model-xhigh",
                "choices": [{"delta": {"content": "ok"}}],
            })
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(_endpoint(), "alias", "private")
    assert result.stream.actual_model_id == "provider-model-xhigh"
    assert result.thinking.levels == ()
    assert result.thinking.inconclusive
    assert not result.thinking_level_locked
    assert result.supports_tools
    assert levels == list(model_probe.PROBE_LEVELS)


def test_every_reasoning_level_is_requested_and_only_observed_levels_are_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    bodies: list[dict[str, object]] = []

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if "tools" in body:
                return _tool_turn(body)
            level = body.get("reasoning_effort")
            if level in ("low", "high", "max"):
                return _event({"choices": [{"delta": {"reasoning_content": "work"}}]})
            if level:
                return httpx.Response(400, json={"error": "unsupported"})
            return _event({"model": "qwen-3.8-max", "choices": [{"delta": {"content": "ok"}}]})
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(_endpoint(), "qwen-3.8-max", "private")
    assert result.thinking.levels == ("low", "high", "max")
    assert result.default_thinking_level == "high"
    assert not result.thinking_level_locked
    assert result.supports_tools
    assert [body.get("reasoning_effort") for body in bodies] == [
        None, *model_probe.PROBE_LEVELS, None, None,
    ]


def test_failed_levels_mark_thinking_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "reasoning_effort" in body:
                return httpx.Response(400, json={"error": "unsupported"})
            return _event({"model": "plain-model", "choices": [{"delta": {"content": "ok"}}]})
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(_endpoint(), "plain-model", "private")
    assert not result.thinking.supported
    assert not result.thinking.inconclusive
    assert result.default_thinking_level is None


def test_tool_call_without_the_file_content_in_the_answer_is_not_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    tool_requests: list[dict[str, Any]] = []

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "tools" not in body:
                return _event({"choices": [{"delta": {"content": "ok"}}]})
            tool_requests.append(body)
            if body["messages"][-1]["role"] == "tool":
                return _event({"choices": [{"delta": {"content": "I read the file."}}]})
            return _read_file_call()
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(_endpoint(), "m", "private")
    assert len(tool_requests) == 2
    assert not result.tools.supported
    assert result.tools.inconclusive


def test_tool_probe_stops_when_the_model_keeps_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    tool_requests: list[dict[str, Any]] = []

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "tools" not in body:
                return _event({"choices": [{"delta": {"content": "ok"}}]})
            tool_requests.append(body)
            return _read_file_call()
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(_endpoint(), "m", "private")
    assert len(tool_requests) == model_probe._TOOL_ROUNDS
    assert not result.tools.supported
    assert result.tools.inconclusive


def test_budget_protocol_does_not_claim_untested_discrete_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    bodies: list[dict[str, object]] = []

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if "thinking" in body:
                return _event({
                    "type": "content_block_start", "content_block": {"type": "thinking"},
                })
            return _event({"type": "message_start"})
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    result = model_probe.probe_model_capabilities(
        _endpoint(ProviderProtocol.ANTHROPIC_MESSAGES), "m", "private"
    )
    assert result.thinking.supported
    assert result.thinking.levels == ()
    assert result.default_thinking_level is None
    assert len([body for body in bodies if "thinking" in body]) == 1


@pytest.fixture(autouse=True)
def fast_probe_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实限流逻辑仍运行；模拟时间避免已有协议测试每次等待 12 秒。"""
    monkeypatch.setattr(model_probe.DEFAULT_RATE_LIMITER, "_clock", count(step=60).__next__)
    monkeypatch.setattr(model_probe.DEFAULT_RATE_LIMITER, "_last", {})


def test_every_probe_http_request_acquires_shared_site_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from limbowave.application.services.endpoint_rate_limiter import EndpointRateLimiter

    now = 0.0
    limiter = EndpointRateLimiter(clock=lambda: now)
    endpoint = _endpoint()
    limiter.configure([endpoint])
    assert limiter.try_acquire(endpoint.id) == 0  # 模拟刚发出的对话请求。
    waits: list[float] = []
    requests: list[httpx.Request] = []

    def advance(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    monkeypatch.setattr(limiter._stopped, "wait", advance)
    original = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            # hook 已占用当前请求的额度，不能在同一时刻发出另一请求。
            assert limiter.try_acquire(endpoint.id) == pytest.approx(12)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "m"}]})
            body = json.loads(request.content)
            if "tools" in body:
                return _tool_turn(body)
            return _event({"choices": [{"delta": {"content": "ok", "reasoning_content": "hmm"}}]})
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", client)
    model_probe.discover_models(endpoint, "private", limiter=limiter)
    result = model_probe.probe_model_capabilities(endpoint, "m", "private", limiter=limiter)
    assert result.alive and result.supports_tools
    # 清单 + 流式 + 每一个思考等级 + 工具调用/结果回传，不能只按模型数限流。
    assert len(requests) == 1 + 1 + len(model_probe.PROBE_LEVELS) + 2
    assert sum(waits) == pytest.approx(12 * len(requests))
