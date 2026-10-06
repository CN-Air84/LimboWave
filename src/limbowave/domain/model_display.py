"""逻辑模型的显示名建议与统一字母排序，不修改 ID 或配置中的路由顺序。"""

from __future__ import annotations

import re
from functools import lru_cache

_ACRONYMS = re.compile(r"(?<![a-z])(glm|gpt)(?![a-z])", re.IGNORECASE)


@lru_cache(maxsize=2048)
def model_name_sort_key(name: str) -> str:
    """显示名按 A–Z 排序：忽略大小写和首尾空白，中文使用无声调拼音。"""
    stripped = name.strip()
    if stripped.isascii():
        return stripped.casefold()
    # Loading the large dictionary is unnecessary for the usual ASCII model IDs
    # and the empty login UI. Non-ASCII names retain the existing pinyin ordering.
    from pypinyin import lazy_pinyin

    return "".join(lazy_pinyin(stripped)).casefold()


def model_display_name(model_id: str) -> str:
    """保留原有连字符转空格/标题格式，并保持 GLM、GPT 缩写全大写。"""
    name = model_id.strip().replace("-", " ").title()
    return _ACRONYMS.sub(lambda match: match.group().upper(), name)
