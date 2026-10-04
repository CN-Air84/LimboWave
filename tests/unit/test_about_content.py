"""关于页内容模型的守卫：默认占位、JSON 往返、合并回退与注入文件发现。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from limbowave.ui.about_content import (
    ABOUT_ENV_VAR,
    ABOUT_FILENAME,
    AboutContent,
    AboutField,
    AboutSection,
    default_content,
    discover_about_file,
    load_embedded,
)


def test_default_content_matches_placeholder_page() -> None:
    content = default_content()
    assert content.tagline.startswith("待填写")
    assert content.notice.startswith("待填写") or content.notice
    assert content.footer
    assert [section.title for section in content.sections] == [
        "版本与更新", "资源与社区", "隐私与数据", "法律与许可",
    ]
    assert sum(len(section.fields) for section in content.sections) == 19
    assert all(
        field.value.startswith("待填写")
        for section in content.sections
        for field in section.fields
    )


def test_dict_round_trip() -> None:
    content = AboutContent(
        tagline="灵波 —— 桌面聊天与 Agent 双核心 Harness。",
        notice="首个测试版。",
        footer="感谢相遇。",
        sections=(
            AboutSection("自定节", "描述", (AboutField("字段甲", "内容甲"),), ("动作甲",)),
        ),
    )
    assert AboutContent.from_dict(content.to_dict()) == content


def test_from_dict_is_tolerant() -> None:
    data = {
        "tagline": "一句话",
        "unknown_key": {"whatever": True},
        "sections": [
            "不是对象",
            {"description": "没有标题，跳过"},
            {
                "title": "正常节",
                "description": 123,  # 类型不对回退空串
                "fields": [
                    ["名称", "内容"],
                    ["缺内容"],
                    ["标签", "内容", "多余元素"],
                    {"label": "字典式", "value": "也支持"},
                    {"label": ""},  # 空标签跳过
                    42,
                ],
                "actions": ["动作", "", None],
            },
        ],
    }
    content = AboutContent.from_dict(data)
    assert content.tagline == "一句话"
    assert content.notice == ""
    assert len(content.sections) == 1
    section = content.sections[0]
    assert section.description == ""
    assert section.fields == (
        AboutField("名称", "内容"),
        AboutField("字典式", "也支持"),
    )
    assert section.actions == ("动作",)


def test_load_rejects_malformed(tmp_path: Path) -> None:
    bad = tmp_path / ABOUT_FILENAME
    bad.write_text("{ 不是 JSON", encoding="utf-8")
    with pytest.raises(ValueError):
        AboutContent.load(bad)
    good = tmp_path / "top-array.json"
    good.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        AboutContent.load(good)


def test_merged_overrides_and_falls_back() -> None:
    base = default_content()
    custom = AboutContent(
        tagline="一句话介绍",
        sections=(
            AboutSection(
                "版本与更新", "", (
                    AboutField("当前版本", "0.0.1"),
                    AboutField("构建信息", ""),  # 空 → 回退默认占位
                ),
            ),
            AboutSection("全新小节", "自定义", (AboutField("自定义字段", "内容"),)),
        ),
    )
    merged = custom.merged(base)
    assert merged.tagline == "一句话介绍"
    assert merged.notice == base.notice  # 未填 → 默认占位
    titles = [section.title for section in merged.sections]
    assert titles[:2] == ["版本与更新", "全新小节"]  # 自定义在前，未覆盖小节补在后
    assert titles[2:] == ["资源与社区", "隐私与数据", "法律与许可"]

    by_title = {section.title: section for section in merged.sections}
    version_section = by_title["版本与更新"]
    fields = {field.label: field.value for field in version_section.fields}
    assert fields["当前版本"] == "0.0.1"
    assert fields["构建信息"].startswith("待填写")  # 空值回退占位
    assert len(version_section.fields) == 5  # 缺失的默认字段整体补齐
    assert version_section.actions == base.sections[0].actions
    assert by_title["全新小节"].fields == (AboutField("自定义字段", "内容"),)


def test_discover_prefers_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / ABOUT_FILENAME
    file.write_text(json.dumps({"tagline": "环境变量版"}), encoding="utf-8")
    monkeypatch.setenv(ABOUT_ENV_VAR, str(file))
    assert discover_about_file() == file


def test_discover_reads_meipass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / ABOUT_FILENAME
    file.write_text("{}", encoding="utf-8")
    monkeypatch.delenv(ABOUT_ENV_VAR, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert discover_about_file() == file


def test_discover_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ABOUT_ENV_VAR, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"))
    assert discover_about_file() is None


def test_load_embedded_falls_back_on_broken_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = tmp_path / ABOUT_FILENAME
    file.write_text("{ 损坏", encoding="utf-8")
    monkeypatch.setenv(ABOUT_ENV_VAR, str(file))
    assert load_embedded() == AboutContent()

    file.write_text(json.dumps({"tagline": "能用"}), encoding="utf-8")
    assert load_embedded().tagline == "能用"
