"""脱敏策略：哪些东西永远不许进入快照、持久化数据与异常文本。

设计约束（Phase 1B 验收 + 设计计划 §13.1）：

- Authorization、API key 与自定义敏感 header **永不**入库，也**不做可逆哈希**。
- 保存的是结构化的"存在性视图"：``{"present": true, "scheme": "Bearer", "value": "[REDACTED]"}``。
- 脱敏在**领域层**定义（策略属于业务规则），由应用层在写快照前调用。

这里刻意不提供"反脱敏"能力——没有原值可还原，这是特性而非缺陷。
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# 认证类请求头：整值替换为结构化视图
SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
    }
)

# 请求体/参数中的敏感字段名（按名称匹配，递归）
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "credential",
    }
)

# 兜底：从任意文本里抹掉看起来像密钥的片段（用于异常文本）
#
# 每个模式都刻意带一个**统一的第一捕获组**（无前缀时为空串）。这样替换逻辑可以无条件
# 读 group(1)，不必按模式区分组数——早先的版本对没有捕获组的模式会直接 IndexError，
# 而这函数崩掉的场合恰好是它最该工作的场合（错误消息里带着密钥）。
_TEXT_PATTERNS = (
    re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9\-._~+/=]{8,}"),
    re.compile(r"()\bsk-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"()\b(?:AKIA|ASIA)[0-9A-Z]{12,}"),
)


def redact_credential(value: str) -> dict[str, Any]:
    """把认证头的值变成不可还原的结构化视图。

    保留 scheme（``Bearer`` / ``Basic``）便于排错——那不是秘密，值才是。
    """
    text = value.strip()
    head, _, rest = text.partition(" ")
    if rest and head.isalpha():
        scheme: str | None = head
    else:
        scheme = None
    return {"present": bool(text), "scheme": scheme, "value": REDACTED}


def is_redacted_view(value: Any) -> bool:
    """判断某个值是否已经是本模块产出的脱敏视图。

    存在意义：脱敏必须**幂等**。扩展会在源头把认证头变成结构化视图，应用侧还要再脱敏
    一次做纵深防御；若不识别"已脱敏"，第二次会把 dict 先 str() 再拆解，scheme 等信息
    就被破坏了——分层防御反而损坏数据。
    """
    return isinstance(value, dict) and value.get("value") == REDACTED


def redact_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    """按名称脱敏请求头。非敏感头原样保留（它们对排错有用）。"""
    if not headers:
        return {}
    out: dict[str, Any] = {}
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADER_NAMES:
            out[name] = value if is_redacted_view(value) else redact_credential(str(value))
        else:
            out[name] = value
    return out


def redact_body(value: Any) -> Any:
    """递归脱敏请求体/响应体：**名字匹配 + 文本模式兜底**，两层都要。

    只按字段名匹配是不够的：模型会把凭据**回显**在正文里（「你的 key 是 sk-…」），
    上游错误消息也会把带 key 的 URL 原样吐回来——那些位置没有敏感的字段名。
    §十三.1 明确要求「加密原始日志保存完整凭据应默认禁止」，所以任何字符串
    都要再过一遍 ``redact_text``。

    代价是正文里看起来像密钥的片段（哪怕是在讨论密钥本身）也会被替换掉。
    这是有意取舍：日志宁可少一段可读文本，也不能留下可用凭据。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_FIELD_NAMES:
                out[key] = REDACTED
            else:
                out[key] = redact_body(item)
        return out
    if isinstance(value, list):
        return [redact_body(item) for item in value]
    if isinstance(value, tuple):
        return [redact_body(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """抹掉任意文本里形似密钥的片段。用于异常与错误消息。"""
    out = text
    for pattern in _TEXT_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    return out
