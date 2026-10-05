"""站点（端点）与凭据引用的领域模型。

设计约束（Phase 1A 验收标准）：
- 端点配置**不含密钥本体**，只含 ``credential_ref`` —— 一个指向加密密钥库的引用。
- ``compat`` 原样透传给 Pi 的 ``models.json``（合同 §五.5 的兼容开关）。
- 模型 id 的协议取值与 Pi 的 ``api`` 字段一一对应。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from limbowave.domain.retry import RetryPolicy


class ProviderProtocol(enum.StrEnum):
    """Pi 支持的 provider 协议（合同 §五.5）。"""

    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"


class RetryConfig(BaseModel):
    """站点的超时与重试策略（§二.4 的站点预设字段 / §八.3 的配置来源）。

    默认面向「偶尔抖一下的连接」：3 次尝试、1s 起、指数退避、上限 8s。
    ``max_attempts=1`` 表示不重试。
    """

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_ms: int = Field(default=1000, ge=0, le=60_000)
    max_delay_ms: int = Field(default=8000, ge=0, le=300_000)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)

    def to_policy(self) -> RetryPolicy:
        """转成领域规则用的策略对象。"""
        from limbowave.domain.retry import RetryPolicy

        return RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay_ms=self.base_delay_ms,
            max_delay_ms=self.max_delay_ms,
            multiplier=self.multiplier,
        )


class EndpointConfig(BaseModel):
    """一个站点端点（中转站 / 官方 / 本地）。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api: ProviderProtocol
    # 密钥引用（指向加密密钥库），不是密钥本体
    credential_ref: str | None = None
    # 默认请求头，可含 $ENV 间接引用（合同 §五.5 的值解析）
    headers: dict[str, str] = Field(default_factory=dict)
    # Pi 兼容开关，原样透传
    compat: dict[str, Any] = Field(default_factory=dict)
    # 超时与连接重试策略（§二.4 / §八.3）：由站点预设控制，不是全局写死
    retry: RetryConfig = Field(default_factory=RetryConfig)
    # 单次请求超时（秒）。None = 交给内核默认值
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    # 参数白名单（§二.4）：只允许这些参数上线。空 = 不限制
    param_whitelist: tuple[str, ...] = ()
    # 参数删除规则（§二.4）：发送前一律去掉这些参数
    strip_params: tuple[str, ...] = ()
    # 已废弃：仅兼容旧配置的读写，不参与路由或自动匹配排序。
    # 优先级改由每个逻辑模型的 bindings 顺序独立维护。
    priority: int = 0
    # 每分钟请求数；同一站点的测活与对话共用额度，平滑发送以避免突发 429。
    rpm: int = Field(default=5, ge=1, le=60_000, strict=True)

    @field_validator("base_url")
    @classmethod
    def _must_be_url(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(f"base_url 必须是 http(s) URL：{value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _rules_consistent(self) -> EndpointConfig:
        """规则自洽性在**构造配置时**就校验——不要等发送时才炸（§二.4）。"""
        from limbowave.domain.param_rules import validate_rules

        validate_rules(whitelist=self.param_whitelist, strip=self.strip_params)
        return self
