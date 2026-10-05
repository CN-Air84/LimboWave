"""流式测活确实区分工具调用和普通文本响应。"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx
import pytest

from limbowave.application.services import model_probe
from limbowave.domain.models import ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol

_ARGS = '{"path": "limbowave-probe.txt"}'


def _sse(*events: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))


def _text(protocol: ProviderProtocol, text: str) -> httpx.Response:
    if protocol is ProviderProtocol.OPENAI_COMPLETIONS:
        return _sse({"choices": [{"delta": {"content": text}}]})
    if protocol is ProviderProtocol.OPENAI_RESPONSES:
        return _sse({
            "type": "response.output_item.done", "output_index": 0,
            "item": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        })
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        return _sse(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}},
        )
    return _sse({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]})


def _read_file_call(protocol: ProviderProtocol) -> httpx.Response:
    """各协议真实形状的 read_file 调用；能分片的参数都拆成两段发。"""
    if protocol is ProviderProtocol.OPENAI_COMPLETIONS:
        return _sse(
            {"choices": [{"delta": {"reasoning_content": "need the file", "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": _ARGS[:10]},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": _ARGS[10:]}},
            ]}}]},
        )
    if protocol is ProviderProtocol.OPENAI_RESPONSES:
        return _sse(
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                      "name": "read_file", "arguments": _ARGS}},
        )
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        return _sse(
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {},
            }},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": _ARGS[:10]}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": _ARGS[10:]}},
            {"type": "content_block_stop", "index": 0},
        )
    return _sse({"candidates": [{"content": {"role": "model", "parts": [{
        "functionCall": {"name": "read_file", "args": {"path": "limbowave-probe.txt"}},
        "thoughtSignature": "sig",
    }]}}]})


def _tool_result(protocol: ProviderProtocol, body: dict[str, Any]) -> str | None:
    """请求最后一条是工具结果时取出其中的文件内容。"""
    if protocol is ProviderProtocol.OPENAI_COMPLETIONS:
        last = body["messages"][-1]
        return last["content"] if last["role"] == "tool" else None
    if protocol is ProviderProtocol.OPENAI_RESPONSES:
        last = body["input"][-1]
        return last["output"] if last.get("type") == "function_call_output" else None
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        content = body["messages"][-1]["content"]
        return content[0]["content"] if isinstance(content, list) else None
    part = body["contents"][-1]["parts"][0]
    return part["functionResponse"]["response"]["content"] if "functionResponse" in part else None


@pytest.mark.parametrize("protocol", list(ProviderProtocol))
def test_probe_round_trips_a_real_tool_call(
    monkeypatch: pytest.MonkeyPatch, protocol: ProviderProtocol
) -> None:
    original = httpx.Client
    tool_bodies: list[dict[str, Any]] = []

    def fake_client(**kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            if protocol in (ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES):
                assert request.headers["authorization"] == "Bearer private-key"
            body = json.loads(request.content)
            if "tools" not in body:
                return _text(protocol, "ok")
            tool_bodies.append(body)
            result = _tool_result(protocol, body)
            if result is None:
                return _read_file_call(protocol)
            return _text(protocol, f"The file contains: {result}")

        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", fake_client)
    endpoint = EndpointConfig(
        id="a", name="A", base_url="https://example.com/v1", api=protocol, credential_ref="secret"
    )
    result = model_probe.probe_model(
        endpoint, ModelBinding(endpoint_id="a", model_id="remote"), "private-key"
    )
    assert result.alive and result.supports_tools
    assert "private-key" not in result.detail
    first, second = tool_bodies
    # 不强制调用、不带思考参数：和普通对话一样由模型自己决定
    assert not {"tool_choice", "toolConfig", "reasoning_effort", "reasoning", "thinking"} & set(
        first
    )
    # 第二轮把模型这一轮的输出原样带回，再附上工具结果
    if protocol is ProviderProtocol.OPENAI_COMPLETIONS:
        assistant, tool = second["messages"][1:]
        assert assistant["tool_calls"] == [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": _ARGS},
        }]
        assert assistant["reasoning_content"] == "need the file"
        assert tool["tool_call_id"] == "call_1"
    elif protocol is ProviderProtocol.OPENAI_RESPONSES:
        assert [item["type"] for item in second["input"][1:]] == [
            "reasoning", "function_call", "function_call_output",
        ]
        assert second["input"][-1]["call_id"] == "call_1"
    elif protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        assert second["messages"][1]["content"] == [{
            "type": "tool_use", "id": "toolu_1", "name": "read_file",
            "input": {"path": "limbowave-probe.txt"},
        }]
        assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    else:
        assert second["contents"][1]["parts"][0]["thoughtSignature"] == "sig"
        assert second["contents"][2]["parts"][0]["functionResponse"]["name"] == "read_file"


def test_probe_does_not_infer_tools_from_successful_text(monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.Client

    def fake_client(**kwargs: object) -> httpx.Client:
        return original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(model_probe.httpx, "Client", fake_client)
    endpoint = EndpointConfig(
        id="a", name="A", base_url="https://example.com/v1", api=ProviderProtocol.OPENAI_COMPLETIONS
    )
    result = model_probe.probe_model(
        endpoint, ModelBinding(endpoint_id="a", model_id="remote"), None
    )
    assert result.alive and not result.supports_tools


def test_missing_secret_never_sends_request() -> None:
    endpoint = EndpointConfig(
        id="a",
        name="A",
        base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
        credential_ref="secret",
    )
    result = model_probe.probe_model(
        endpoint, ModelBinding(endpoint_id="a", model_id="remote"), None
    )
    assert not result.alive and not result.supports_tools


def test_stream_error_is_not_reported_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.Client

    def fake_client(**kwargs: object) -> httpx.Client:
        return original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, text='data: {"type":"error","error":{"message":"private-key"}}\n\n'
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(model_probe.httpx, "Client", fake_client)
    endpoint = EndpointConfig(
        id="a",
        name="A",
        base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    result = model_probe.probe_model(
        endpoint, ModelBinding(endpoint_id="a", model_id="remote"), None
    )
    assert not result.alive and not result.supports_tools
    assert "private-key" not in result.detail


def test_tool_rejection_keeps_stream_result_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.Client
    bodies: list[dict[str, object]] = []

    def fake_client(**kwargs: object) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if "tools" in body:
                return httpx.Response(400, text="unsupported tools")
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')

        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(model_probe.httpx, "Client", fake_client)
    endpoint = EndpointConfig(
        id="a",
        name="A",
        base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    result = model_probe.probe_model(
        endpoint, ModelBinding(endpoint_id="a", model_id="remote"), None
    )
    assert result.alive and not result.supports_tools
    assert "HTTP 400" in result.detail
    assert len(bodies) == 2
    assert "tools" not in bodies[0]
    assert "tool_choice" not in bodies[1]


@pytest.fixture(autouse=True)
def fast_probe_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实限流逻辑仍运行；模拟时间避免已有协议测试每次等待 12 秒。"""
    monkeypatch.setattr(model_probe.DEFAULT_RATE_LIMITER, "_clock", count(step=60).__next__)
    monkeypatch.setattr(model_probe.DEFAULT_RATE_LIMITER, "_last", {})
