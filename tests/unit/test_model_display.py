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
