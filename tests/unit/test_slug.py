from __future__ import annotations

import pytest

from limbowave.domain.slug import slugify, unique_slug


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("中转站 A", "zhong-zhuan-zhan-a"),
        ("OpenRouter", "openrouter"),
        ("DeepSeek 官方", "deepseek-guan-fang"),
        ("硅基流动(SiliconFlow)", "gui-ji-liu-dong-siliconflow"),
        ("relay-a", "relay-a"),
        ("  ", "endpoint"),
        ("🚀", "endpoint"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_unique_slug_appends_suffix() -> None:
    assert unique_slug("Relay", set()) == "relay"
    assert unique_slug("Relay", {"relay", "relay-2"}) == "relay-3"
