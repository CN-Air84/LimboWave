"""Composer grouping is independent of routing and supports aliased logical models."""

import pytest

from limbowave.domain.model_brand import MODEL_BRANDS, model_brand


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("DeepSeek-V3.2", "Deepseek"),
        ("deepseek-ai/DeepSeek-R1", "Deepseek"),
        ("Qwen3-235B", "Qwen"),
        ("通义千问", "Qwen"),
        ("moonshot/kimi-k2", "Kimi"),
        ("moonshot-v1-128k", "Kimi"),
        ("z-ai/GLM-4.5", "GLM"),
        ("智谱", "GLM"),
        ("MiniMax-M2", "MiniMax"),
        ("xiaomi/MiMo-V2-Flash", "Mimo"),
        ("step-3.5-flash", "StepFun"),
        ("STEP3", "StepFun"),
        ("stepfun-ai/step-3", "StepFun"),
        ("阶跃星辰", "StepFun"),
        ("SenseNova-V6", "SenseNova"),
        ("商汤日日新", "SenseNova"),
        ("anthropic/claude-sonnet-4", "Claude"),
        ("google/GEMINI-2.5-pro", "Gemini"),
        ("openai/gpt-5", "GPT"),
        ("o3-mini", "GPT"),
        ("x-ai/grok-4", "Grok"),
        ("local/custom-chat", "其他"),
        ("footsteps", "其他"),
        ("stepwise-helper", "其他"),
        ("", "其他"),
    ],
)
def test_model_brand_keywords(value, expected):
    assert model_brand(value) == expected


def test_classifier_precedence_and_binding_fallback():
    assert model_brand("logical-id", "快速模型", ("vendor/step-3",)) == "StepFun"
    assert model_brand("logical-id", "Claude 主力", ("gpt-5",)) == "Claude"
    assert model_brand("deepseek-r1-distill-qwen", "GPT alias") == "Deepseek"
    assert model_brand("logical-id", "常用", ("custom", "GLM-5")) == "GLM"


def test_requested_brand_order_is_stable():
    assert MODEL_BRANDS == (
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
