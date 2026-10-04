"""自动重试的领域规则验收（§八.3）。

**核心是「不重试什么」**——重试错了会重复副作用或丢用户已看到的内容。
因此这里逐条钉住计划书的两个清单：

可重试：DNS 临时失败、建连失败、连接重置、未产内容的流中断、明确网络超时。
不可重试：鉴权失败、参数错误、限流、业务错误、已产生副作用的工具调用、
已输出大量内容后的流中断。
"""

from __future__ import annotations

import pytest

from limbowave.domain.retry import (
    RETRYABLE,
    ErrorClass,
    RetryAttempt,
    RetryPolicy,
    classify,
    describe_class,
    is_retryable,
)

# ---------- 可重试：连接类 ----------


@pytest.mark.parametrize(
    "message",
    [
        "getaddrinfo ENOTFOUND api.example.com",
        "EAI_AGAIN temporary DNS failure",
        "connect ECONNREFUSED 127.0.0.1:443",
        "read ECONNRESET",
        "socket hang up",
        "Connection reset by peer",
        "connection refused",
    ],
)
def test_connection_errors_are_retryable(message: str) -> None:
    assert classify(message=message) is ErrorClass.CONNECTION
    assert is_retryable(classify(message=message))


@pytest.mark.parametrize(
    "message",
    ["ETIMEDOUT", "request timed out", "socket timeout after 30s"],
)
def test_timeouts_are_retryable(message: str) -> None:
    assert classify(message=message) is ErrorClass.TIMEOUT
    assert is_retryable(classify(message=message))


def test_stream_interrupted_before_content_is_retryable() -> None:
    """还没吐出内容就断了：重试是安全的。"""
    result = classify(message="stream interrupted: ECONNRESET", had_content=False)
    assert result is ErrorClass.CONNECTION  # 连接类优先（更具体）
    assert is_retryable(result)


# ---------- 不可重试：不会变好 ----------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_not_retryable(status: int) -> None:
    result = classify(message="provider rejected", status=status)
    assert result is ErrorClass.AUTH
    assert not is_retryable(result)


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_param_errors_not_retryable(status: int) -> None:
    result = classify(message="bad request", status=status)
    assert result is ErrorClass.PARAM
    assert not is_retryable(result)


def test_rate_limit_not_retryable() -> None:
    """限流不自动重试（计划书明确列出；重试只会更糟）。"""
    result = classify(message="429 Too Many Requests", status=429)
    assert result is ErrorClass.RATE_LIMIT
    assert not is_retryable(result)

    by_text = classify(message="rate limit exceeded")
    assert by_text is ErrorClass.RATE_LIMIT


def test_business_error_not_retryable() -> None:
    result = classify(message="model returned invalid response", status=500)
    assert not is_retryable(result)


def test_text_level_auth_and_rate_limit() -> None:
    """没有状态码时靠文本识别（Pi 的错误串里常带这些词）。"""
    assert classify(message="invalid api key provided") is ErrorClass.AUTH
    assert classify(message="You exceeded your current quota") is ErrorClass.RATE_LIMIT


# ---------- 不可重试：重试有代价 ----------


def test_tool_side_effect_never_retried() -> None:
    """工具已产生副作用：**即使错误是连接类也不重试**——重试会真的再做一遍。"""
    result = classify(message="ECONNRESET", tools_ran=True)
    assert result is ErrorClass.TOOL_SIDE_EFFECT
    assert not is_retryable(result)


def test_stream_break_after_output_not_retried() -> None:
    """已输出大量内容后中断：重试等于丢弃用户已看到的内容。"""
    result = classify(message="stream interrupted", had_content=True)
    assert result is ErrorClass.STREAM_AFTER_OUTPUT
    assert not is_retryable(result)


def test_content_with_connection_error_still_retried() -> None:
    """有内容但错误是纯连接类（不是流中断）：仍可重试。

    区分点在 `_looks_like_stream_break`——不是「有内容就一律不重试」。
    """
    result = classify(message="ECONNREFUSED", had_content=True)
    assert result is ErrorClass.CONNECTION
    assert is_retryable(result)


# ---------- 判定顺序 ----------


def test_tool_side_effect_beats_auth_classification() -> None:
    """顺序：副作用优先于状态码分类（保守优先）。"""
    assert classify(message="unauthorized", status=401, tools_ran=True) is (
        ErrorClass.TOOL_SIDE_EFFECT
    )


def test_unknown_is_not_retryable() -> None:
    """认不出来就当不可重试——宁可少重试，不可乱重试。"""
    result = classify(message="something strange happened")
    assert result is ErrorClass.UNKNOWN
    assert not is_retryable(result)


def test_retryable_set_matches_plan() -> None:
    """可重试集合必须**恰好**是计划书列的三类连接问题。"""
    assert {
        ErrorClass.CONNECTION,
        ErrorClass.TIMEOUT,
        ErrorClass.STREAM_INTERRUPTED_EARLY,
    } == RETRYABLE


# ---------- 策略：有限次数退避 ----------


def test_policy_limits_attempts() -> None:
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(attempt=1, error_class=ErrorClass.CONNECTION)
    assert policy.should_retry(attempt=2, error_class=ErrorClass.CONNECTION)
    assert not policy.should_retry(attempt=3, error_class=ErrorClass.CONNECTION)  # 用尽


def test_policy_never_retries_non_retryable() -> None:
    policy = RetryPolicy(max_attempts=10)
    for error_class in (ErrorClass.AUTH, ErrorClass.PARAM, ErrorClass.RATE_LIMIT):
        assert not policy.should_retry(attempt=1, error_class=error_class)


def test_policy_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(base_delay_ms=1000, multiplier=2.0, max_delay_ms=8000)
    assert policy.delay_ms(attempt=1) == 1000
    assert policy.delay_ms(attempt=2) == 2000
    assert policy.delay_ms(attempt=3) == 4000
    assert policy.delay_ms(attempt=4) == 8000
    assert policy.delay_ms(attempt=5) == 8000  # 封顶


def test_policy_can_disable_retry() -> None:
    policy = RetryPolicy(max_attempts=1)
    assert not policy.enabled
    assert not policy.should_retry(attempt=1, error_class=ErrorClass.CONNECTION)


@pytest.mark.parametrize("bad", [0, -1])
def test_policy_rejects_invalid_attempts(bad: int) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=bad)


def test_policy_rejects_negative_delays() -> None:
    with pytest.raises(ValueError, match="退避时间"):
        RetryPolicy(base_delay_ms=-1)


# ---------- 尝试记录（要显示在消息里） ----------


def test_attempt_describes_readably() -> None:
    attempt = RetryAttempt(
        attempt=2, error_class=ErrorClass.CONNECTION, message="ECONNRESET", status=None
    )
    text = attempt.describe
    assert "第 2 次" in text
    assert "连接失败" in text


def test_attempt_includes_status_when_present() -> None:
    attempt = RetryAttempt(attempt=1, error_class=ErrorClass.RATE_LIMIT, message="429", status=429)
    assert "429" in attempt.describe


def test_all_classes_have_labels() -> None:
    """每个分类都要有中文标签——否则界面会露出英文枚举值。"""
    for error_class in ErrorClass:
        assert describe_class(error_class)
        assert describe_class(error_class) != error_class.value or error_class is (
            ErrorClass.UNKNOWN
        )
