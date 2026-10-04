from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QPushButton
from pytestqt.qtbot import QtBot

from limbowave.ui import theme
from limbowave.ui.about_content import ABOUT_ENV_VAR, AboutContent, AboutField, AboutSection
from limbowave.ui.about_page import AboutPage

EXPECTED_SECTIONS = (
    "版本与更新", "资源与社区",
    "隐私与数据", "法律与许可",
)


def test_about_page_has_rich_explicit_placeholders(qtbot: QtBot) -> None:
    page = AboutPage()
    qtbot.addWidget(page)
    labels = page.findChildren(QLabel)
    headings = [label.text() for label in labels if label.property("aboutRole") == "sectionTitle"]
    assert headings == list(EXPECTED_SECTIONS)
    placeholders = [label for label in labels if label.property("aboutRole") == "value"]
    assert len(placeholders) == 19
    fields = {label.text() for label in labels if label.property("aboutRole") == "fieldTitle"}
    assert "设计贡献" not in fields
    assert not {"项目维护者", "开发团队", "联系邮箱"} & fields
    assert all(label.text().startswith("待填写") for label in placeholders)
    assert all(label.wordWrap() for label in labels)
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    assert all(not label.openExternalLinks() for label in labels)
    assert all(
        label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard
        for label in placeholders
    )
    actions = page.findChildren(QPushButton)
    assert len(actions) == 5
    assert not any(
        removed in button.text()
        for button in actions
        for removed in ("提交反馈", "查看第三方声明", "了解参与方式")
    )
    assert all(not button.isEnabled() and "待接入" in button.text() for button in actions)
    assert all(button.toolTip() for button in actions)


@pytest.mark.parametrize(("width", "scale"), [(760, 1.0), (360, 1.0), (420, 1.4), (360, 1.6)])
def test_about_page_scrolls_without_horizontal_clipping(
    qtbot: QtBot, width: int, scale: float,
) -> None:
    previous_scale = theme.current_font_scale()
    try:
        theme.set_font_scale(scale)
        page = AboutPage()
        qtbot.addWidget(page)
        page.resize(width, 480)
        page.show()
        qtbot.waitExposed(page)
        qtbot.waitUntil(lambda: page.verticalScrollBar().maximum() > 0)
        assert page.horizontalScrollBar().maximum() == 0
        assert page.widget().width() <= page.viewport().width()
        for label in page.findChildren(QLabel):
            page.ensureWidgetVisible(label, 0, 0)
            point = label.mapTo(page.viewport(), QPoint(0, 0))
            rect = label.rect().translated(point)
            assert rect.left() >= 0
            assert rect.right() < page.viewport().width()
            assert label.height() >= label.heightForWidth(label.width())
        footer = page.findChild(QLabel, "aboutFooter")
        assert footer is not None
        page.ensureWidgetVisible(footer)
        assert page.viewport().rect().contains(
            footer.rect().translated(footer.mapTo(page.viewport(), QPoint(0, 0)))
        )
        page.setFocus()
        before = page.verticalScrollBar().value()
        qtbot.keyClick(page, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClick(page, Qt.Key.Key_PageUp)
        assert page.verticalScrollBar().value() < before
    finally:
        theme.set_font_scale(previous_scale)


def test_about_page_renders_injected_content(qtbot: QtBot) -> None:
    content = AboutContent(
        tagline="灵波一句话",
        notice="提示一句",
        footer="页脚一句",
        sections=(
            AboutSection(
                "自定节", "自定描述",
                (AboutField("字段甲", "内容甲"), AboutField("空字段", "")),
            ),
        ),
    )
    page = AboutPage(content=content)
    qtbot.addWidget(page)
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "灵波一句话" in texts
    assert "提示一句" in texts
    assert "页脚一句" in texts
    assert "内容甲" in texts
    assert "待填写" in texts  # 空值自定义字段回退占位
    headings = [
        label.text() for label in page.findChildren(QLabel)
        if label.property("aboutRole") == "sectionTitle"
    ]
    assert headings[0] == "自定节"
    assert "版本与更新" in headings  # 未覆盖的默认小节按原样补在后面


def test_about_page_loads_env_injected_file(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = tmp_path / "about.json"
    file.write_text(json.dumps({"tagline": "环境变量注入"}), encoding="utf-8")
    monkeypatch.setenv(ABOUT_ENV_VAR, str(file))
    page = AboutPage()
    qtbot.addWidget(page)
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "环境变量注入" in texts
    # 未填写的条目仍是默认占位文案
    placeholders = [
        label for label in page.findChildren(QLabel)
        if label.property("aboutRole") == "value"
    ]
    assert all(label.text().startswith("待填写") for label in placeholders)


def test_about_page_restyles_existing_content(qtbot: QtBot) -> None:
    previous_palette = theme.current_palette()
    previous_scale = theme.current_font_scale()
    try:
        page = AboutPage()
        qtbot.addWidget(page)
        for palette in ("light", "dark"):
            theme.set_palette(palette)
            theme.set_font_scale(1.2)
            page.restyle()
            assert theme.BG_SURFACE in page.styleSheet()
            assert theme.TEXT_PRIMARY in page.styleSheet()
            assert f"font-size: {theme.FS_BASE}px" in page.styleSheet()
            assert f"font-size: {theme.FS_TITLE * 2}px" in page.styleSheet()
    finally:
        theme._install_palette(previous_palette)
        theme.set_font_scale(previous_scale)
