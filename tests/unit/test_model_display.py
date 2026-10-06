"""Shared display ordering and suggested names never change stable model IDs."""

import pytest

from limbowave.domain.model_display import model_display_name, model_name_sort_key


def test_names_sort_case_insensitively_with_chinese_pinyin():
    names = ["Zulu", "深度模型", "beta", "通义", "Alpha", "阿尔法", "apple"]
    assert sorted(names, key=model_name_sort_key) == [
        "阿尔法",
        "Alpha",
        "apple",
        "beta",
        "深度模型",
        "通义",
        "Zulu",
    ]


def test_sort_ignores_outer_whitespace_and_preserves_same_name_order():
    assert model_name_sort_key(" alpha ") == model_name_sort_key("ALPHA")
    assert sorted(["beta", "Beta", "alpha"], key=model_name_sort_key) == [
        "alpha",
        "beta",
        "Beta",
    ]


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("glm-5.3", "GLM 5.3"),
        ("GLM-4.5-air", "GLM 4.5 Air"),
        ("gPt-5-mini", "GPT 5 Mini"),
        ("openai/gpt-5", "Openai/GPT 5"),
        ("z-ai/glm-5.3", "Z Ai/GLM 5.3"),
        ("glm4", "GLM4"),
        ("gpt5", "GPT5"),
        ("gpt_5", "GPT_5"),
        ("  MY-chat-model  ", "My Chat Model"),
        ("deepseek-chat", "Deepseek Chat"),
        ("mygptmodel", "Mygptmodel"),
        ("", ""),
    ],
)
def test_suggested_name_preserves_brand_initialisms(model_id, expected):
    assert model_display_name(model_id) == expected


def test_ascii_sort_does_not_load_pinyin_dictionary():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; "
            "from limbowave.domain.model_display import model_name_sort_key; "
            "assert model_name_sort_key(' GPT-5 ') == 'gpt-5'; "
            "assert 'pypinyin' not in sys.modules"
        )], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_sort_key_cache_is_bounded_and_reuses_chinese_names():
    model_name_sort_key.cache_clear()
    first = model_name_sort_key("通义千问")
    assert model_name_sort_key("通义千问") == first
    assert model_name_sort_key.cache_info().hits == 1
    for index in range(3000):
        model_name_sort_key(f"model-{index}")
    assert model_name_sort_key.cache_info().currsize <= 2048
