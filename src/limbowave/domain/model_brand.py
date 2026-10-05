"""按名称关键字给模型选择菜单分组；不参与模型绑定、能力推断或路由。"""

from __future__ import annotations

import re
from collections.abc import Iterable

MODEL_BRANDS = (
    "Deepseek",
    "Qwen",
    "Kimi",
    "GLM",
    "MiniMax",
    "Mimo",
    "StepFun",
    "SenseNova",
    "Claude",
    "Gemini",
    "GPT",
    "Grok",
    "其他",
)

_PATTERNS = tuple(
    (brand, re.compile(pattern, re.IGNORECASE))
    for brand, pattern in (
        ("Deepseek", r"deep[\s_-]*seek|深度求索"),
        ("Qwen", r"qwen|通义|千问"),
        ("Kimi", r"kimi|moonshot|月之暗面"),
        ("GLM", r"glm|智谱"),
        ("MiniMax", r"mini[\s_-]*max"),
        ("Mimo", r"mimo"),
        ("StepFun", r"(?:^|[^a-z0-9])step(?:fun|(?=$|[-_/.\s\d]))|阶跃"),
        ("SenseNova", r"sense[\s_-]*nova|商汤|日日新"),
        ("Claude", r"claude|anthropic"),
        ("Gemini", r"gemini"),
        ("GPT", r"gpt|openai|(?:^|[/\s])o[134](?:$|[-.\s])"),
        ("Grok", r"grok"),
    )
)


def model_brand(
    model_id: str,
    display_name: str = "",
    actual_model_ids: Iterable[str] = (),
) -> str:
    """优先逻辑 ID，其次显示名，最后按绑定顺序尝试实际 ID；未命中也不能丢模型。"""
    for value in (model_id, display_name, *actual_model_ids):
        for brand, pattern in _PATTERNS:
            if pattern.search(value.strip()):
                return brand
    return "其他"
