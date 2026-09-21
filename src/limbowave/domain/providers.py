"""站点（端点）与凭据引用的领域模型。

设计约束（Phase 1A 验收标准）：
- 端点配置**不含密钥本体**，只含 ``credential_ref`` —— 一个指向加密密钥库的引用。
- ``compat`` 原样透传给 Pi 的 ``models.json``（合同 §五.5 的兼容开关）。
- 模型 id 的协议取值与 Pi 的 ``api`` 字段一一对应。
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderProtocol(enum.StrEnum):
    """Pi 支持的 provider 协议（合同 §五.5）。"""

    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"


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

    @field_validator("base_url")
    @classmethod
    def _must_be_url(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(f"base_url 必须是 http(s) URL：{value!r}")
        return value.rstrip("/")
