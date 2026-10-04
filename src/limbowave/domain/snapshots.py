"""请求双快照：应用意图 与 最终传输。

两者的区别是本阶段的验收重点（ADR-0001 裁决 1）：

- ``RequestIntentSnapshot``：**应用准备发送什么**。在调用内核前生成，包含逻辑模型、
  选定端点、路由原因、消息引用与应用级参数。
- ``TransportSnapshot``：**Provider 实际收到什么**。由 ``before_provider_request`` /
  ``before_provider_headers`` / ``after_provider_response`` 观测得到。

二者刻意不合并：Pi 会注入 system 消息、补默认参数、加流式开关，这些差异本身就是要记录的事实。
一轮 run 可能产生多条 ``TransportSnapshot``（工具调用会多次往返），用 ``sequence`` 排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RequestIntentSnapshot:
    """应用侧意图。生成后不可变。"""

    id: str
    run_id: str
    conversation_id: str
    branch_id: str
    logical_model_id: str
    endpoint_id: str
    routing_reason: str
    created_at: datetime
    prompt_layers: dict[str, str] = field(default_factory=dict)
    message_ids: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()
    app_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransportSnapshot:
    """Provider 侧实际请求与响应。**密钥已脱敏，且不可还原。**"""

    id: str
    run_id: str
    created_at: datetime
    sequence: int = 0
    attempt: int = 1
    provider: str | None = None
    model_id: str | None = None
    url: str | None = None
    # 脱敏后的请求头：敏感头为 {"present":bool,"scheme":str|None,"value":"[REDACTED]"}
    headers: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    # 意图参数与实际上线参数的键级差异
    param_diff: dict[str, Any] = field(default_factory=dict)
    response_status: int | None = None
    stop_reason: str | None = None
    error_class: str | None = None
    # 「原始响应」（§十三.1）：**解析后的响应对象**，不是 provider 的线上字节。
    # Pi 的 after_provider_response 只给 status + headers（已核对 types.d.ts），
    # 线上原文没有暴露给扩展，因此这里存的是 message_end 上的完整助手消息对象
    # （content 块、usage、stopReason、errorMessage）。这一点在界面上如实标注。
    response_body: dict[str, Any] = field(default_factory=dict)
    # 「流式事件」（§十三.1）：见 domain/stream_tape.py（紧凑序列 + 计数 + 截断量）
    stream_tape: dict[str, Any] = field(default_factory=dict)


def diff_params(intent_params: dict[str, Any], transport_body: dict[str, Any]) -> dict[str, Any]:
    """按键比较应用意图参数与实际上线请求体。

    只报键的存在性差异，不比较值——值会随会话内容变化，不是"参数来源"问题。
    这是设计计划 §4.4「不允许运行时私自注入来源不明的字段」的可观测化。
    """
    intent_keys = set(intent_params)
    transport_keys = set(transport_body)
    return {
        "only_in_intent": sorted(intent_keys - transport_keys),
        "only_in_transport": sorted(transport_keys - intent_keys),
        "shared": sorted(intent_keys & transport_keys),
    }
