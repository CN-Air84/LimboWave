"""站点参数规则验收（§二.4「参数白名单与删除规则」）。

锁住的不变量：
- 顺序固定：**先删除规则，再白名单裁剪**，两个清单分开返回（可解释）；
- 关键参数（model/messages/...）**不允许**被规则拿掉——删掉请求就没意义了；
- 白名单与删除规则**不得重叠**（语义矛盾，构造时就拒）；
- 空规则 = 不改写请求（不引入无谓的改写）。
"""

from __future__ import annotations

import pytest

from limbowave.domain.param_rules import (
    PROTECTED_KEYS,
    ParamRuleError,
    apply_param_rules,
    rules_active,
    validate_rules,
)

PAYLOAD = {
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 4096,
    "temperature": 0.7,
    "reasoning_effort": "high",
}


def test_strip_removes_only_listed_keys() -> None:
    filtered, stripped, dropped = apply_param_rules(PAYLOAD, strip=("reasoning_effort",))
    assert "reasoning_effort" not in filtered
    assert stripped == ("reasoning_effort",)
    assert dropped == ()
    assert filtered["max_tokens"] == 4096  # 其他参数原样


def test_whitelist_keeps_only_allowed() -> None:
    filtered, stripped, dropped = apply_param_rules(
        PAYLOAD, whitelist=("model", "messages", "max_tokens")
    )
    assert set(filtered) == {"model", "messages", "max_tokens"}
    assert "temperature" in dropped
    assert "reasoning_effort" in dropped
    assert stripped == ()


def test_strip_applies_before_whitelist() -> None:
    """顺序：先删除规则、再白名单——两个清单要能分别说清是谁拿掉的。"""
    filtered, stripped, dropped = apply_param_rules(
        PAYLOAD, whitelist=("model", "messages", "temperature"), strip=("temperature",)
    )
    # temperature 虽然**在白名单里**，仍被删除规则拿掉——且记在 stripped 而不是
    # dropped 里（两个清单分开就是为了说清「是谁拿掉的」）
    assert "temperature" not in filtered
    assert stripped == ("temperature",)
    # 不在白名单里的参数照常被裁
    assert set(dropped) == {"max_tokens", "reasoning_effort"}


def test_values_are_never_modified() -> None:
    """规则只增删**键**，绝不改值——值必须可追溯（§四.4）。"""
    filtered, _, _ = apply_param_rules(PAYLOAD, whitelist=("model", "messages", "max_tokens"))
    assert filtered["max_tokens"] == PAYLOAD["max_tokens"]
    assert filtered["messages"] == PAYLOAD["messages"]


def test_protected_keys_survive_strip() -> None:
    """关键参数不允许被删除规则拿掉。"""
    filtered, stripped, _ = apply_param_rules(PAYLOAD, strip=("model", "messages"))
    assert "model" in filtered
    assert "messages" in filtered
    assert stripped == ()


def test_protected_keys_survive_whitelist() -> None:
    """白名单没写关键参数时，**在请求体里的**那些也不会被裁掉（兜底）。

    注意语义：关键参数是「存在则保留」，不是「凭空补上」——
    规则不改写请求的语义，只增删键。
    """
    filtered, _, _ = apply_param_rules(PAYLOAD, whitelist=("max_tokens",))
    present_protected = PROTECTED_KEYS & set(PAYLOAD)
    for key in present_protected:
        assert key in filtered
    assert "contents" not in filtered  # 本来就没有的不凭空补


def test_empty_rules_are_noop() -> None:
    filtered, stripped, dropped = apply_param_rules(PAYLOAD)
    assert filtered == PAYLOAD
    assert stripped == () and dropped == ()
    assert not rules_active(whitelist=(), strip=())


def test_rules_active_detects_configuration() -> None:
    assert rules_active(whitelist=("a",), strip=())
    assert rules_active(whitelist=(), strip=("a",))


# ---------- 规则自洽性（构造配置时就该拦住） ----------


def test_overlapping_rules_rejected() -> None:
    with pytest.raises(ParamRuleError, match="语义矛盾"):
        validate_rules(whitelist=("temperature",), strip=("temperature",))


def test_stripping_protected_key_rejected() -> None:
    with pytest.raises(ParamRuleError, match="关键参数"):
        validate_rules(whitelist=(), strip=("messages",))


def test_whitelist_missing_protected_rejected() -> None:
    """白名单漏掉 messages/model → 请求失去意义，构造时就拒。"""
    with pytest.raises(ParamRuleError, match="必须包含关键参数"):
        validate_rules(whitelist=("max_tokens",), strip=())


def test_endpoint_config_validates_rules_at_construction() -> None:
    """EndpointConfig 构造时就校验——不要等发送时才炸。"""
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    with pytest.raises(ValueError, match="语义矛盾"):
        EndpointConfig(
            id="bad",
            name="bad",
            base_url="https://x.example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
            param_whitelist=("temperature",),
            strip_params=("temperature",),
        )


def test_endpoint_config_accepts_valid_rules() -> None:
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    endpoint = EndpointConfig(
        id="relay",
        name="中转站",
        base_url="https://relay.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
        strip_params=("reasoning_effort",),
        priority=5,
    )
    assert endpoint.strip_params == ("reasoning_effort",)
    assert endpoint.priority == 5


# ---------- 站点-模型级能力声明（§二.4 模型级覆盖） ----------


def test_binding_carries_capability_declarations() -> None:
    """思考强度默认值与工具声明在**绑定**上（站点-模型），不是逻辑模型上。"""
    from limbowave.domain.models import LogicalModel, ModelBinding

    binding = ModelBinding(
        endpoint_id="relay-a",
        model_id="deepseek-chat",
        default_thinking_level="high",
        supports_tools=True,
    )
    model = LogicalModel(id="deepseek-chat", name="DeepSeek", bindings=[binding])
    assert model.bindings[0].default_thinking_level == "high"
    assert model.bindings[0].supports_tools is True
    # 逻辑模型本身不带这些字段——能力属于「某站点上的某模型」
    assert not hasattr(model, "default_thinking_level")
    assert not hasattr(model, "supports_tools")


def test_binding_defaults_are_conservative() -> None:
    """默认不声明思考强度、不声明工具支持——宁可不假设。"""
    from limbowave.domain.models import ModelBinding

    binding = ModelBinding(endpoint_id="e", model_id="m")
    assert binding.default_thinking_level is None
    assert binding.supports_tools is False
