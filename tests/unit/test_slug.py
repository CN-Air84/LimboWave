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


def test_ascii_slug_does_not_load_pinyin_dictionary():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; from limbowave.domain.slug import unique_slug; "
            "assert unique_slug('Open AI', {'open-ai'}) == 'open-ai-2'; "
            "assert 'pypinyin' not in sys.modules"
        )], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
