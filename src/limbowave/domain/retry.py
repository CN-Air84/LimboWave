"""自动重试的领域规则（§八.3）。

**这个模块的核心价值是「不重试什么」**——重试错了会重复副作用或浪费用户额度。

只自动重试**连接类**错误：

- DNS 临时失败、建连失败、连接重置；
- 流在**未产生有效内容前**异常中断；
- 明确的网络超时。

**不自动重试**（无论多少次都不会变好，或者重试有代价）：

- 鉴权失败、参数错误、限流、服务端业务错误；
- **已产生副作用的工具调用**——重试会真的再做一遍；
- **已输出大量内容后的流中断**——重试等于丢弃用户已看到的内容。

另外两条工程约束（计划书明确要求）：

- **有限次数退避**：次数与等待时间由**站点预设**配置（不是全局写死）；
- **在消息内显示**：用户要能看见「正在第 N 次重试、还要等多久」。

分类与策略都是**纯函数**，不碰网络也不碰内核——这样边界才能被测试钉住。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class ErrorClass(enum.StrEnum):
    """一次失败的分类。是否可重试由 :data:`RETRYABLE` 决定。"""

    # ---- 可重试：连接类 ----
    CONNECTION = "connection"  # DNS / 建连失败 / 连接重置
    TIMEOUT = "timeout"  # 明确的网络超时
    STREAM_INTERRUPTED_EARLY = "stream_interrupted_early"  # 未产生有效内容前中断

    # ---- 不可重试：不会变好 ----
    AUTH = "auth"
    PARAM = "param"
    RATE_LIMIT = "rate_limit"
    BUSINESS = "business"  # 服务端业务错误（4xx/5xx 非连接类）

    # ---- 不可重试：重试有代价 ----
    TOOL_SIDE_EFFECT = "tool_side_effect"  # 工具已产生副作用
    STREAM_AFTER_OUTPUT = "stream_after_output"  # 已输出大量内容后中断

    UNKNOWN = "unknown"


RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.CONNECTION, ErrorClass.TIMEOUT, ErrorClass.STREAM_INTERRUPTED_EARLY}
)

# 连接类错误的文本特征。Pi 与底层 fetch/Node 的错误串混在一起，
# 因此按关键词识别而不是只看异常类型。
_CONNECTION_PATTERNS = (
    re.compile(r"ECONNREFUSED|ECONNRESET|EPIPE|EHOSTUNREACH|ENETUNREACH", re.IGNORECASE),
    re.compile(r"ENOTFOUND|EAI_AGAIN|getaddrinfo|name resolution", re.IGNORECASE),
    re.compile(r"connection (refused|reset|closed|error|failed)", re.IGNORECASE),
    re.compile(r"socket hang up", re.IGNORECASE),
    re.compile(r"dns", re.IGNORECASE),
)
_TIMEOUT_PATTERNS = (
    re.compile(r"ETIMEDOUT|ESOCKETTIMEDOUT", re.IGNORECASE),
    re.compile(r"timed?\s*out", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
)
_AUTH_PATTERNS = (re.compile(r"unauthor|forbidden|invalid api key|authentication", re.IGNORECASE),)
_RATE_LIMIT_PATTERNS = (re.compile(r"rate.?limit|too many requests|quota", re.IGNORECASE),)

# HTTP 状态码的分类（比文本更可靠，优先用）
_AUTH_STATUS = frozenset({401, 403})
_PARAM_STATUS = frozenset({400, 404, 405, 409, 422})


def classify(
    *,
    message: str,
    status: int | None = None,
    had_content: bool = False,
    tools_ran: bool = False,
) -> ErrorClass:
    """把一次失败归类。**纯函数**，判定顺序固定且可解释。

    顺序（重要）：

    1. **已产生副作用的工具** → 一律不可重试（重试会真的再执行一遍工具）；
    2. **已输出内容后的中断** → 不可重试（重试等于丢弃用户已看到的内容）；
    3. HTTP 状态码（鉴权 / 参数 / 限流）→ 明确分类；
    4. 文本特征（连接 / 超时）→ 可重试；
    5. 其余 → 业务错误（不可重试）。
    """
    if tools_ran:
        return ErrorClass.TOOL_SIDE_EFFECT

    text = message or ""

    # 流中断：区分「还没吐出东西」与「已经输出了一大段」
    if had_content and _looks_like_stream_break(text):
        return ErrorClass.STREAM_AFTER_OUTPUT

    if status is not None:
        if status in _AUTH_STATUS:
            return ErrorClass.AUTH
        if status == 429:
            return ErrorClass.RATE_LIMIT
        if status in _PARAM_STATUS:
            return ErrorClass.PARAM

    if any(p.search(text) for p in _AUTH_PATTERNS):
        return ErrorClass.AUTH
    if any(p.search(text) for p in _RATE_LIMIT_PATTERNS):
        return ErrorClass.RATE_LIMIT
    if any(p.search(text) for p in _CONNECTION_PATTERNS):
        return ErrorClass.CONNECTION
    if any(p.search(text) for p in _TIMEOUT_PATTERNS):
        return ErrorClass.TIMEOUT

    if had_content:
        # 有内容但不像连接问题：当作业务性中断，不重试
        return ErrorClass.BUSINESS

    return ErrorClass.UNKNOWN


def _looks_like_stream_break(message: str) -> bool:
    """是否是「流中断」类错误（而不是正常的业务失败）。"""
    patterns = (
        re.compile(r"stream", re.IGNORECASE),
        re.compile(r"aborted|abort", re.IGNORECASE),
        re.compile(r"ECONNRESET|EPIPE|socket hang up", re.IGNORECASE),
        re.compile(r"interrupted", re.IGNORECASE),
    )
    return any(p.search(message) for p in patterns)


def is_retryable(error_class: ErrorClass) -> bool:
    return error_class in RETRYABLE


# ---------- 重试策略（由站点预设配置，§八.3） ----------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """有限次数退避。次数与等待时间来自**站点预设**，不是全局写死。

    默认值面向「偶尔抖一下的连接」：3 次尝试、1s 起、指数退避、上限 8s。
    ``max_attempts=1`` 表示不重试（只有首次尝试）。
    """

    max_attempts: int = 3
    base_delay_ms: int = 1000
    max_delay_ms: int = 8000
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.base_delay_ms < 0 or self.max_delay_ms < 0:
            raise ValueError("退避时间不得为负")

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 1

    def should_retry(self, *, attempt: int, error_class: ErrorClass) -> bool:
        """``attempt`` 是**已完成的尝试次数**（从 1 开始）。"""
        if not is_retryable(error_class):
            return False
        return attempt < self.max_attempts

    def delay_ms(self, *, attempt: int) -> int:
        """第 ``attempt`` 次失败后要等多久（指数退避，封顶 max_delay_ms）。"""
        if attempt < 1:
            return 0
        raw = self.base_delay_ms * (self.multiplier ** (attempt - 1))
        return int(min(raw, self.max_delay_ms))


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """一次尝试的记录。**要显示在消息里，也要进请求日志**（§八.3/§十三.1）。"""

    attempt: int  # 第几次尝试（从 1 开始）
    error_class: ErrorClass
    message: str
    status: int | None = None

    @property
    def describe(self) -> str:
        """给用户看的一行。"""
        label = _CLASS_LABEL.get(self.error_class, self.error_class.value)
        suffix = f"（HTTP {self.status}）" if self.status else ""
        return f"第 {self.attempt} 次尝试失败：{label}{suffix}"


_CLASS_LABEL: dict[ErrorClass, str] = {
    ErrorClass.CONNECTION: "连接失败",
    ErrorClass.TIMEOUT: "网络超时",
    ErrorClass.STREAM_INTERRUPTED_EARLY: "流中断（尚无内容）",
    ErrorClass.AUTH: "鉴权失败",
    ErrorClass.PARAM: "参数错误",
    ErrorClass.RATE_LIMIT: "被限流",
    ErrorClass.BUSINESS: "服务端错误",
    ErrorClass.TOOL_SIDE_EFFECT: "工具已产生副作用",
    ErrorClass.STREAM_AFTER_OUTPUT: "已输出内容后中断",
    ErrorClass.UNKNOWN: "未知错误",
}


def describe_class(error_class: ErrorClass) -> str:
    return _CLASS_LABEL.get(error_class, error_class.value)
