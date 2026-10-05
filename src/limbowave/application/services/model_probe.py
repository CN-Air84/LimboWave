"""站点模型发现与能力探测。

网络调用保持同步，供应用层通过 ``asyncio.to_thread`` 执行；UI 不直接碰网络。
探测顺序固定为：普通流式 → 逐级思考 → 工具调用。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Event
from typing import Any
from urllib.parse import quote

import httpx

from limbowave.application.services.endpoint_rate_limiter import (
    DEFAULT_RATE_LIMITER,
    EndpointRateLimiter,
)
from limbowave.domain.models import ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol

PROBE_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True, slots=True)
class ModelProbeProgress:
    completed: int
    total: int


def model_probe_request_total(endpoint: EndpointConfig) -> int:
    """预留协议对应的请求上限；工具轮数/基础失败导致的跳过在结束时扣除。"""
    thinking = len(PROBE_LEVELS) if endpoint.api in (
        ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES,
    ) else 1
    return 1 + thinking + _TOOL_ROUNDS


class _ProbeProgress:
    def __init__(
        self, endpoint: EndpointConfig, callback: Callable[[ModelProbeProgress], None] | None,
    ) -> None:
        self.completed = 0
        self.total = model_probe_request_total(endpoint)
        self.callback = callback
        self._notify()

    def _notify(self) -> None:
        if self.callback is not None:
            self.callback(ModelProbeProgress(self.completed, self.total))

    def advance(self) -> None:
        self.completed += 1
        self._notify()

    def finish(self) -> None:
        # 不把跳过的请求冒充已完成请求，也不让正常提前结束停在未满进度。
        if self.total != self.completed:
            self.total = self.completed
            self._notify()

@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    models: tuple[DiscoveredModel, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class StreamProbeResult:
    alive: bool
    detail: str
    actual_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class ThinkingProbeResult:
    supported: bool
    level: str | None
    detail: str
    levels: tuple[str, ...] = ()
    inconclusive: bool = False


@dataclass(frozen=True, slots=True)
class ToolProbeResult:
    supported: bool
    alive: bool
    detail: str
    inconclusive: bool = False


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """旧测活 API 的兼容结果。"""

    alive: bool
    supports_tools: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ModelProbeResult:
    model_id: str
    stream: StreamProbeResult
    thinking: ThinkingProbeResult
    tools: ToolProbeResult

    @property
    def default_thinking_level(self) -> str | None:
        return self.thinking.level if self.thinking.supported else None

    @property
    def supports_tools(self) -> bool:
        return self.tools.supported

    @property
    def thinking_level_locked(self) -> bool:
        # 仅靠模型名或单个成功等级无法证明端点固定了思考强度。
        return False

    @property
    def alive(self) -> bool:
        return self.stream.alive

    @property
    def detail(self) -> str:
        return "；".join(
            part
            for part in (
                self.stream.detail,
                self.thinking.detail,
                self.tools.detail,
            )
            if part
        )


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    endpoint: EndpointConfig
    model_id: str
    secret: str | None


def _auth_headers(endpoint: EndpointConfig, secret: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not secret:
        return headers
    if endpoint.api in (ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES):
        headers["Authorization"] = f"Bearer {secret}"
    elif endpoint.api is ProviderProtocol.ANTHROPIC_MESSAGES:
        headers["x-api-key"] = secret
        headers["anthropic-version"] = "2023-06-01"
    elif endpoint.api is ProviderProtocol.GOOGLE_GENERATIVE_AI:
        headers["x-goog-api-key"] = secret
    return headers


def _model_url(endpoint: EndpointConfig) -> str:
    return endpoint.base_url.rstrip("/") + "/models"


def _model_id(value: object) -> str:
    text = str(value or "").strip()
    return text.removeprefix("models/")


def _parse_models(endpoint: EndpointConfig, payload: object) -> tuple[DiscoveredModel, ...]:
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("data") or payload.get("models")
    if not isinstance(raw, list):
        return ()
    result: list[DiscoveredModel] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = _model_id(item.get("id") or item.get("name"))
        if not model_id or model_id in seen:
            continue
        # Google 的 displayName 是实际可读名称，其余协议通常使用 id。
        name = str(item.get("displayName") or item.get("name") or model_id)
        result.append(DiscoveredModel(model_id, name))
        seen.add(model_id)
    return tuple(result)


def _limited_client(
    endpoint: EndpointConfig, limiter: EndpointRateLimiter, cancelled: Event | None,
) -> httpx.Client:
    def before_request(_request: httpx.Request) -> None:
        limiter.wait(endpoint, cancelled=cancelled)

    return httpx.Client(
        timeout=20.0, follow_redirects=False, event_hooks={"request": [before_request]},
    )


def discover_models(
    endpoint: EndpointConfig, secret: str | None, *,
    limiter: EndpointRateLimiter = DEFAULT_RATE_LIMITER, cancelled: Event | None = None,
) -> DiscoveryResult:
    """拉取端点模型清单，不记录密钥，也不把响应正文放进错误消息。"""
    if endpoint.credential_ref and not secret:
        return DiscoveryResult((), "找不到端点密钥")
    try:
        with _limited_client(endpoint, limiter, cancelled) as client:
            response = client.get(_model_url(endpoint), headers=_auth_headers(endpoint, secret))
        if response.status_code != 200:
            return DiscoveryResult((), f"模型清单请求失败（HTTP {response.status_code}）")
        models = _parse_models(endpoint, response.json())
        return DiscoveryResult(models, f"已发现 {len(models)} 个模型")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return DiscoveryResult((), "模型清单请求失败，请检查 URL、密钥与协议")


# 工具探测：让模型用 read_file 读一个虚拟文件，再在回答里复述文件内容。
_READ_FILE: dict[str, Any] = {
    "name": "read_file",
    "description": "Read a text file and return its contents.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path of the file to read"}},
        "required": ["path"],
    },
}
_TOOL_PROMPT = (
    "Use the read_file tool to read limbowave-probe.txt, then reply with its exact contents."
)
# 模型拿到结果后还继续调用工具时照常回应，最多请求这么多轮。
_TOOL_ROUNDS = 3


def _tool_history(endpoint: EndpointConfig) -> list[dict[str, Any]]:
    if endpoint.api is ProviderProtocol.GOOGLE_GENERATIVE_AI:
        return [{"role": "user", "parts": [{"text": _TOOL_PROMPT}]}]
    return [{"role": "user", "content": _TOOL_PROMPT}]


def _tool_request(
    endpoint: EndpointConfig, model_id: str, secret: str | None, history: list[dict[str, Any]]
) -> tuple[str, dict[str, str], dict[str, object]]:
    # 不强制 tool_choice、不带思考参数，和普通对话一样由模型自己决定调用；也不限输出长度：
    # 推理模型的思考计入上限，设小了会在发起调用前被截断（Anthropic 必填，给足）。
    base = endpoint.base_url.rstrip("/")
    headers = _auth_headers(endpoint, secret)
    if endpoint.api is ProviderProtocol.OPENAI_RESPONSES:
        # Responses 的函数工具可能默认严格模式，严格模式要求参数结构封闭。
        parameters = {**_READ_FILE["parameters"], "additionalProperties": False}
        return (
            base + "/responses",
            headers,
            {
                "model": model_id,
                "input": history,
                "tools": [{"type": "function", **_READ_FILE, "parameters": parameters}],
                "stream": True,
            },
        )
    if endpoint.api is ProviderProtocol.OPENAI_COMPLETIONS:
        return (
            base + "/chat/completions",
            headers,
            {
                "model": model_id,
                "messages": history,
                "tools": [{"type": "function", "function": _READ_FILE}],
                "stream": True,
            },
        )
    if endpoint.api is ProviderProtocol.ANTHROPIC_MESSAGES:
        return (
            base + "/messages",
            headers,
            {
                "model": model_id,
                "messages": history,
                "tools": [
                    {
                        "name": _READ_FILE["name"],
                        "description": _READ_FILE["description"],
                        "input_schema": _READ_FILE["parameters"],
                    }
                ],
                "stream": True,
                "max_tokens": 4096,
            },
        )
    url = base + "/models/" + quote(model_id, safe="") + ":streamGenerateContent?alt=sse"
    return url, headers, {"contents": history, "tools": [{"functionDeclarations": [_READ_FILE]}]}


def _plain_request(
    endpoint: EndpointConfig, model_id: str, secret: str | None, *, level: str | None = None
) -> tuple[str, dict[str, str], dict[str, object]]:
    base = endpoint.base_url.rstrip("/")
    headers = _auth_headers(endpoint, secret)
    prompt = "Reply with ok."
    if endpoint.api is ProviderProtocol.OPENAI_RESPONSES:
        body: dict[str, object] = {
            "model": model_id,
            "input": prompt,
            "stream": True,
            "max_output_tokens": 512 if level else 64,
        }
        if level is not None:
            body["reasoning"] = {"effort": level}
        return base + "/responses", headers, body
    if endpoint.api is ProviderProtocol.OPENAI_COMPLETIONS:
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": 512,
        }
        if level is not None:
            body["reasoning_effort"] = level
        return base + "/chat/completions", headers, body
    if endpoint.api is ProviderProtocol.ANTHROPIC_MESSAGES:
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": 2048 if level is not None else 128,
        }
        if level is not None:
            body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        return base + "/messages", headers, body
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {}}
    if level is not None:
        body["generationConfig"] = {"thinkingConfig": {"thinkingBudget": 1024}}
    return (
        base + "/models/" + quote(model_id, safe="") + ":streamGenerateContent?alt=sse",
        headers,
        body,
    )


def _events(response: httpx.Response) -> Iterator[dict[str, object]]:
    size = 0
    for line in response.iter_lines():
        size += len(line)
        # 只防失控：工具探测不限输出长度，推理模型逐字流出的思考就可能有几百 KB。
        if size > 4_194_304:
            break
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def _has_reasoning(event: dict[str, object]) -> bool:
    event_type = str(event.get("type") or "")
    if "reason" in event_type.lower() or "thinking" in event_type.lower():
        return True
    for block in (event.get("content_block"), event.get("delta")):
        if isinstance(block, dict) and block.get("type") in (
            "thinking", "thinking_delta", "redacted_thinking"
        ):
            return True
    choices = event.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if isinstance(choice, dict):
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict) and (
                delta.get("reasoning") or delta.get("reasoning_content")
            ):
                return True
    content = event.get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking")
        for block in content
    ):
        return True
    candidates = event.get("candidates")
    for candidate in candidates if isinstance(candidates, list) else []:
        if isinstance(candidate, dict):
            candidate_content = candidate.get("content")
            parts = (
                candidate_content.get("parts", []) if isinstance(candidate_content, dict) else []
            )
            if isinstance(parts, list) and any(
                isinstance(part, dict) and part.get("thought") for part in parts
            ):
                return True
    return False


def _actual_model_id(event: dict[str, object]) -> str | None:
    direct = event.get("model") or event.get("modelVersion")
    if direct:
        return _model_id(direct)
    message = event.get("message")
    if isinstance(message, dict) and message.get("model"):
        return _model_id(message.get("model"))
    response = event.get("response")
    if isinstance(response, dict) and response.get("model"):
        return _model_id(response.get("model"))
    return None


def _stream_result(
    response: httpx.Response,
    *,
    predicate: Callable[[dict[str, object]], bool] | None = None,
) -> tuple[bool, bool, str, str | None]:
    if response.status_code != 200:
        return False, False, f"请求失败（HTTP {response.status_code}）", None
    seen = False
    matched = False
    actual_model_id: str | None = None
    for event in _events(response):
        if event.get("error") or event.get("type") in ("error", "response.failed"):
            return False, False, "模型返回错误", actual_model_id
        seen = True
        actual_model_id = _actual_model_id(event) or actual_model_id
        if predicate is not None and predicate(event):
            matched = True
    if not seen:
        return False, False, "没有收到有效的流式响应", actual_model_id
    return True, matched, "流式响应成功", actual_model_id


def _request_stream(
    client: httpx.Client,
    request: tuple[str, dict[str, str], dict[str, object]],
    predicate: Callable[[dict[str, object]], bool] | None = None,
    *, progress: _ProbeProgress | None = None,
) -> tuple[bool, bool, str, str | None]:
    url, headers, body = request
    try:
        with client.stream("POST", url, headers=headers, json=body) as response:
            result = _stream_result(response, predicate=predicate)
    except (httpx.HTTPError, ValueError):
        result = False, False, "请求异常，请检查站点连接", None
    # 收完流并关闭响应后才计数；RPM 等待/取消不等同于完成请求。
    if progress is not None:
        progress.advance()
    return result


def _probe_thinking(
    client: httpx.Client, endpoint: EndpointConfig, model_id: str, secret: str | None,
    stream: StreamProbeResult, *, progress: _ProbeProgress | None = None,
) -> ThinkingProbeResult:
    if not stream.alive:
        return ThinkingProbeResult(False, None, "基础流式失败，未测试思考", inconclusive=True)
    if endpoint.api not in (ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES):
        # 这些协议使用 token budget，不支持逐级 effort；不能把同一个预算冒充多个等级。
        alive, observed, detail, _ = _request_stream(
            client, _plain_request(endpoint, model_id, secret, level="high"), _has_reasoning,
            progress=progress,
        )
        supported = alive and observed
        return ThinkingProbeResult(
            supported, None,
            "已验证预算式思考；协议未提供离散 effort 等级"
            if supported else f"预算思考未确认：{detail}",
            (), not supported,
        )
    candidates = PROBE_LEVELS
    available: list[str] = []
    uncertain: list[str] = []
    rejected: list[str] = []
    for level in candidates:
        alive, observed, detail, _ = _request_stream(
            client, _plain_request(endpoint, model_id, secret, level=level), _has_reasoning,
            progress=progress,
        )
        if alive and observed:
            available.append(level)
        elif alive or not detail.startswith("请求失败（HTTP 4"):
            uncertain.append(level)
        else:
            rejected.append(level)
    default = "high" if "high" in available else (available[0] if available else None)
    summary = "已验证请求等级：" + ("、".join(available) if available else "无")
    if available:
        summary += "（不同请求值可能映射到同一实际强度）"
    if rejected:
        summary += "；拒绝：" + "、".join(rejected)
    if uncertain:
        summary += "；未确认：" + "、".join(uncertain)
    return ThinkingProbeResult(bool(available), default, summary, tuple(available), bool(uncertain))


@dataclass(frozen=True, slots=True)
class _ToolCall:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class _ToolTurn:
    """一轮带工具的请求：模型发起的调用、回答文本，以及原样放回对话的助手消息。"""

    calls: tuple[_ToolCall, ...]
    text: str
    reply: tuple[dict[str, Any], ...]


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _completions_turn(events: list[dict[str, object]]) -> _ToolTurn:
    text = reasoning = ""
    calls: dict[int, dict[str, Any]] = {}
    for event in events:
        for choice in _dicts(event.get("choices")):
            delta = _dict(choice.get("delta") or choice.get("message"))
            text += str(delta.get("content") or "")
            reasoning += str(delta.get("reasoning_content") or "")
            for position, part in enumerate(_dicts(delta.get("tool_calls"))):
                index = part.get("index")
                call = calls.setdefault(
                    index if isinstance(index, int) else position,
                    {"type": "function", "function": {"name": "", "arguments": ""}},
                )
                # 调用按 index 分片到达：arguments 逐段拼接，其余字段（含厂商扩展）保留最新值。
                function = _dict(part.get("function"))
                call["function"]["name"] = function.get("name") or call["function"]["name"]
                call["function"]["arguments"] += str(function.get("arguments") or "")
                call.update(
                    {k: v for k, v in part.items() if v and k not in ("index", "function")}
                )
    tool_calls = [calls[index] for index in sorted(calls)]
    for number, call in enumerate(tool_calls):
        call.setdefault("id", f"call_{number}")
        call["function"]["arguments"] = call["function"]["arguments"] or "{}"
    message: dict[str, Any] = {
        "role": "assistant", "content": text or None, "tool_calls": tool_calls,
    }
    if reasoning:
        # 部分推理模型要求调用工具的轮次带回思考内容。
        message["reasoning_content"] = reasoning
    return _ToolTurn(
        tuple(_ToolCall(call["id"], call["function"]["name"]) for call in tool_calls),
        text,
        (message,),
    )


def _responses_turn(events: list[dict[str, object]]) -> _ToolTurn:
    items: dict[int, dict[str, Any]] = {}
    for event in events:
        item, response = event.get("item"), _dict(event.get("response"))
        if event.get("type") == "response.output_item.done" and isinstance(item, dict):
            index = event.get("output_index")
            items[index if isinstance(index, int) else len(items)] = item
        elif event.get("type") == "response.completed" and _dicts(response.get("output")):
            # 完成事件带着完整输出时以它为准。
            items = dict(enumerate(_dicts(response.get("output"))))
    # 推理条目也要原样带回：推理模型的函数调用离不开它前面的推理条目。
    output = tuple(items[index] for index in sorted(items))
    return _ToolTurn(
        tuple(
            _ToolCall(str(item.get("call_id") or ""), str(item.get("name") or ""))
            for item in output
            if item.get("type") == "function_call"
        ),
        "".join(
            str(part.get("text") or "")
            for item in output
            if item.get("type") == "message"
            for part in _dicts(item.get("content"))
        ),
        output,
    )


def _anthropic_turn(events: list[dict[str, object]]) -> _ToolTurn:
    blocks: dict[int, dict[str, Any]] = {}
    for event in events:
        index, start, delta = event.get("index"), event.get("content_block"), event.get("delta")
        if not isinstance(index, int):
            continue
        if isinstance(start, dict):
            blocks[index] = dict(start)
        elif isinstance(delta, dict) and index in blocks:
            # 文本、思考、签名和工具参数都是逐段追加的字符串。
            for key in ("text", "thinking", "signature", "partial_json"):
                if isinstance(delta.get(key), str):
                    blocks[index][key] = str(blocks[index].get(key) or "") + delta[key]
    content: list[dict[str, Any]] = []
    for block in (blocks[index] for index in sorted(blocks)):
        if "partial_json" in block:
            try:
                block["input"] = json.loads(block.pop("partial_json") or "{}")
            except ValueError:
                block["input"] = {}
        # 思考块（含签名）要原样带回；空文本块会被接口拒收。
        if block.get("type") != "text" or block.get("text"):
            content.append(block)
    return _ToolTurn(
        tuple(
            _ToolCall(str(block.get("id") or ""), str(block.get("name") or ""))
            for block in content
            if block.get("type") == "tool_use"
        ),
        "".join(str(block.get("text") or "") for block in content if block.get("type") == "text"),
        ({"role": "assistant", "content": content},),
    )


def _google_turn(events: list[dict[str, object]]) -> _ToolTurn:
    # 片段原样带回：新模型要求函数调用片段上的 thoughtSignature 一并回传。
    parts = [
        part
        for event in events
        for candidate in _dicts(event.get("candidates"))[:1]
        for part in _dicts(_dict(candidate.get("content")).get("parts"))
    ]
    calls = [_dict(part.get("functionCall")) for part in parts]
    return _ToolTurn(
        tuple(
            _ToolCall(str(call.get("id") or ""), str(call.get("name") or ""))
            for call in calls
            if call
        ),
        "".join(str(part.get("text") or "") for part in parts if not part.get("thought")),
        ({"role": "model", "parts": parts},),
    )


def _tool_results(
    endpoint: EndpointConfig, calls: tuple[_ToolCall, ...], content: str
) -> list[dict[str, Any]]:
    """每个调用都回应同一份文件内容。"""
    if endpoint.api is ProviderProtocol.OPENAI_RESPONSES:
        return [
            {"type": "function_call_output", "call_id": call.id, "output": content}
            for call in calls
        ]
    if endpoint.api is ProviderProtocol.OPENAI_COMPLETIONS:
        return [{"role": "tool", "tool_call_id": call.id, "content": content} for call in calls]
    if endpoint.api is ProviderProtocol.ANTHROPIC_MESSAGES:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": content}
                    for call in calls
                ],
            }
        ]
    return [
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": call.name,
                        "response": {"content": content},
                        **({"id": call.id} if call.id else {}),
                    }
                }
                for call in calls
            ],
        }
    ]


def _request_tool_turn(
    client: httpx.Client, endpoint: EndpointConfig, model_id: str, secret: str | None,
    history: list[dict[str, Any]], *, progress: _ProbeProgress | None = None,
) -> tuple[_ToolTurn | None, str]:
    events: list[dict[str, object]] = []

    def collect(event: dict[str, object]) -> bool:
        events.append(event)
        return False

    alive, _, detail, _ = _request_stream(
        client, _tool_request(endpoint, model_id, secret, history), collect, progress=progress,
    )
    if not alive:
        return None, detail
    parse = {
        ProviderProtocol.OPENAI_COMPLETIONS: _completions_turn,
        ProviderProtocol.OPENAI_RESPONSES: _responses_turn,
        ProviderProtocol.ANTHROPIC_MESSAGES: _anthropic_turn,
    }.get(endpoint.api, _google_turn)
    return parse(events), detail


def _probe_tools(
    client: httpx.Client, endpoint: EndpointConfig, model_id: str, secret: str | None,
    stream: StreamProbeResult, *, progress: _ProbeProgress | None = None,
) -> ToolProbeResult:
    """让模型调用 read_file 读一个虚拟文件并复述内容；复述对了才算支持工具调用。"""
    if not stream.alive:
        return ToolProbeResult(False, False, "基础流式失败，未测试工具", True)
    # 文件内容每次随机生成：模型只能从工具结果里拿到，猜不出来。
    content = secrets.token_hex(4)
    history = _tool_history(endpoint)
    called = False
    for _ in range(_TOOL_ROUNDS):
        turn, detail = _request_tool_turn(
            client, endpoint, model_id, secret, history, progress=progress,
        )
        if turn is None:
            stage = "回传工具结果" if called else "带工具的请求"
            return ToolProbeResult(False, called, f"{stage}：{detail}", True)
        if not turn.calls:
            if not called:
                return ToolProbeResult(False, True, "模型没有调用工具", True)
            if content in turn.text.lower():
                return ToolProbeResult(True, True, "模型调用 read_file 读取文件并复述了内容")
            return ToolProbeResult(False, True, "模型调用了工具，但回答里没有文件内容", True)
        called = True
        history += [*turn.reply, *_tool_results(endpoint, turn.calls, content)]
    return ToolProbeResult(False, True, "模型反复调用工具，没有给出回答", True)


def probe_model_capabilities(
    endpoint: EndpointConfig, model_id: str, secret: str | None, *,
    limiter: EndpointRateLimiter = DEFAULT_RATE_LIMITER, cancelled: Event | None = None,
    on_progress: Callable[[ModelProbeProgress], None] | None = None,
) -> ModelProbeResult:
    progress = _ProbeProgress(endpoint, on_progress)
    if endpoint.credential_ref and not secret:
        failed = StreamProbeResult(False, "找不到端点密钥")
        return ModelProbeResult(
            model_id, failed,
            ThinkingProbeResult(False, None, "未测思考", inconclusive=True),
            ToolProbeResult(False, False, "未测工具", True),
        )
    with _limited_client(endpoint, limiter, cancelled) as client:
        alive, _, detail, actual_model_id = _request_stream(
            client, _plain_request(endpoint, model_id, secret), progress=progress,
        )
        stream = StreamProbeResult(alive, detail, actual_model_id)
        # 探测请求复用相同密钥，但结果与响应正文始终不包含密钥。
        thinking = _probe_thinking(client, endpoint, model_id, secret, stream, progress=progress)
        tools = _probe_tools(client, endpoint, model_id, secret, stream, progress=progress)
    progress.finish()
    return ModelProbeResult(model_id, stream, thinking, tools)


def probe_model(
    endpoint: EndpointConfig, binding: ModelBinding, secret: str | None, *,
    limiter: EndpointRateLimiter = DEFAULT_RATE_LIMITER, cancelled: Event | None = None,
) -> ProbeResult:
    """兼容旧调用方：复用与实际模型页面相同的工具探测流程。"""
    if endpoint.credential_ref and not secret:
        return ProbeResult(False, False, "找不到端点密钥")
    with _limited_client(endpoint, limiter, cancelled) as client:
        alive, _, detail, actual_model_id = _request_stream(
            client, _plain_request(endpoint, binding.model_id, secret)
        )
        if not alive:
            return ProbeResult(False, False, detail)
        tools = _probe_tools(
            client, endpoint, binding.model_id, secret,
            StreamProbeResult(True, detail, actual_model_id),
        )
        return ProbeResult(True, tools.supported, tools.detail)
