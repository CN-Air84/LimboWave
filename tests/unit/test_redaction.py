"""脱敏策略的单元测试。

最重要的一条是**幂等性**：扩展在源头脱敏，应用侧再脱敏一次做纵深防御。
若第二层不认识第一层的产物，就会把结构化视图 stringify 后重新拆解，
scheme 之类信息被破坏——分层防御反而损坏数据（这条是实机集成时才暴露出来的）。
"""

from __future__ import annotations

import json

from limbowave.domain.redaction import (
    REDACTED,
    redact_body,
    redact_credential,
    redact_headers,
    redact_text,
)

SECRET = "sk-live-abc123SECRET"


def test_redact_credential_keeps_scheme_only() -> None:
    assert redact_credential(f"Bearer {SECRET}") == {
        "present": True,
        "scheme": "Bearer",
        "value": REDACTED,
    }
    assert redact_credential(SECRET) == {"present": True, "scheme": None, "value": REDACTED}
    assert redact_credential("") == {"present": False, "scheme": None, "value": REDACTED}


def test_redact_headers_masks_sensitive_only() -> None:
    out = redact_headers(
        {
            "Authorization": f"Bearer {SECRET}",
            "X-Api-Key": SECRET,
            "Cookie": "session=abc",
            "Content-Type": "application/json",
            "X-Tenant": "demo",
        }
    )
    for name in ("Authorization", "X-Api-Key", "Cookie"):
        assert out[name]["value"] == REDACTED
    # 非敏感头原样保留：它们对排错有用
    assert out["Content-Type"] == "application/json"
    assert out["X-Tenant"] == "demo"


def test_redact_body_is_recursive() -> None:
    out = redact_body(
        {
            "model": "x",
            "api_key": SECRET,
            "nested": {"client_secret": SECRET, "ok": 1},
            "items": [{"access_token": SECRET}, "plain"],
        }
    )
    assert out["model"] == "x"
    assert out["api_key"] == REDACTED
    assert out["nested"]["client_secret"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["items"][0]["access_token"] == REDACTED
    assert out["items"][1] == "plain"


def test_redact_headers_is_idempotent() -> None:
    once = redact_headers({"Authorization": f"Bearer {SECRET}", "X-Tenant": "demo"})
    twice = redact_headers(once)
    assert twice == once
    assert twice["Authorization"]["scheme"] == "Bearer", "二次脱敏不得丢失 scheme"


def test_redact_body_is_idempotent() -> None:
    once = redact_body({"api_key": SECRET, "nested": {"token": SECRET}})
    twice = redact_body(once)
    assert twice == once


def test_redact_text_masks_key_like_fragments() -> None:
    assert SECRET not in redact_text(f"401 unauthorized: Bearer {SECRET}")
    assert SECRET not in redact_text(SECRET)
    assert redact_text("普通错误消息") == "普通错误消息"


def test_redact_body_scrubs_credentials_echoed_in_text() -> None:
    """字段名匹配挡不住模型回显（§十三.1）：正文里的凭据也要抹掉。

    这条是补出来的——早先 ``redact_body`` 只看字段名，所以
    「你的 key 是 sk-…」这种内容会原样进日志。
    """
    out = redact_body(
        {
            "content": [{"type": "text", "text": f"你的 key 是 {SECRET}"}],
            "errorMessage": f"upstream 401 for https://api.x/v1?key={SECRET}",
        }
    )
    blob = json.dumps(out, ensure_ascii=False)
    assert SECRET not in blob
    assert "[REDACTED]" in blob
    # 名字层的行为不变（值是定点标记，不是文本替换）
    assert redact_body({"token": SECRET})["token"] == REDACTED
    assert redact_body({"model": "gpt-4o", "n": 3}) == {"model": "gpt-4o", "n": 3}


def test_redact_body_text_scrubbing_is_idempotent() -> None:
    once = redact_body({"text": f"Bearer {SECRET}"})
    assert redact_body(once) == once


def test_no_reversible_artifact() -> None:
    """脱敏视图里只有固定三键与字面量标记，不夹带原值，也不夹带任何指纹。"""
    out = redact_headers({"Authorization": f"Bearer {SECRET}"})
    view = out["Authorization"]

    assert set(view) == {"present", "scheme", "value"}
    assert view["value"] == REDACTED
    assert SECRET not in repr(out)
    # 除 scheme 外不得出现任何由原值派生的字符串（长度、片段、摘要都算）
    for key, value in view.items():
        if key == "scheme":
            continue
        assert value in (True, False, None, REDACTED)
