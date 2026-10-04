"""显示名 → 标识符（站点 id / 密钥引用名）。

站点表单只让用户填显示名，id 与密钥引用都由它派生：中文转拼音，
其余字符归一成小写字母、数字与连字符。派生是纯函数，重名消解由调用方
提供已占用集合。
"""

from __future__ import annotations

import re
from collections.abc import Collection

from pypinyin import lazy_pinyin

_FALLBACK = "endpoint"


def slugify(name: str) -> str:
    """``"中转站 A"`` → ``"zhong-zhuan-zhan-a"``；派生为空时回退 ``endpoint``。"""
    # lazy_pinyin 把每个汉字拆成一个音节，非汉字片段原样保留为一段
    joined = "-".join(lazy_pinyin(name.strip()))
    slug = re.sub(r"[^a-z0-9]+", "-", joined.lower()).strip("-")
    return slug or _FALLBACK


def unique_slug(name: str, taken: Collection[str]) -> str:
    """派生 slug；与 ``taken`` 冲突时依次追加 ``-2``、``-3``……"""
    base = slugify(name)
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
