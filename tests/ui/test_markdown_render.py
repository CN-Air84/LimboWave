"""Markdown 渲染器验收（Task 3.1）。

锁住的不变量：
- 纯函数、绝不抛——空串、半个代码块、未知语言都返回合法 HTML；
- 代码块有语法高亮（span 内联样式）和背景板；
- 标签配对平衡（Qt 富文本对坏 HTML 会静默丢内容）；
- 原始 HTML 被转义（``html=False``）——模型输出里的 <script> 不得执行。
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from limbowave.ui.markdown_render import render

_TAGS = (
    "div",
    "table",
    "pre",
    "code",
    "p",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "span",
    "em",
    "strong",
    "a",
)


def _assert_balanced(html: str) -> None:
    for tag in _TAGS:
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = html.count(f"</{tag}>")
        assert opens == closes, f"<{tag}> 不配对：{opens} 开 vs {closes} 关"


def test_plain_paragraph() -> None:
    html = render("你好，**世界**")
    assert "<strong>" in html
    _assert_balanced(html)


def test_code_block_highlighted_and_balanced() -> None:
    html = render("```python\ndef f():\n    return 1\n```")
    assert "<span style=" in html  # pygments 内联高亮
    assert "bgcolor=" in html  # 背景板
    _assert_balanced(html)


def test_unclosed_fence_is_safe() -> None:
    """流式中途的半个围栏代码块：合法 HTML，不抛。"""
    html = render("```python\ndef unfinished(:")
    assert html
    _assert_balanced(html)


def test_unknown_language_falls_back_to_plain() -> None:
    html = render("```no-such-lang\nxyz\n```")
    assert "xyz" in html
    _assert_balanced(html)


def test_empty_and_whitespace() -> None:
    assert render("")
    assert render("   \n  ")
    _assert_balanced(render(""))


def test_raw_html_is_escaped() -> None:
    """模型输出里的原始 HTML 不得被解析——转义为文本。"""
    html = render("正常 <script>alert(1)</script> 结束")
    assert "<script>" not in html
    assert "script" in html  # 以文本形式保留
    _assert_balanced(html)


def test_table_and_strikethrough() -> None:
    html = render("| A | B |\n|---|---|\n| 1 | 2 |\n\n~~删除~~")
    assert "<td>" in html
    assert "<s>" in html
    _assert_balanced(html)


def test_inline_code_styled() -> None:
    html = render("使用 `pip install` 安装")
    assert "font-family" in html
    _assert_balanced(html)


@pytest.mark.parametrize(
    "emoji",
    [
        "🙂",
        "🥹",
        "👩🏽‍💻",
        "🏳‍🌈",
        "❤‍🔥",
        "👨‍👩‍👧‍👦",
        "🇨🇳",
        "🏳️‍🌈",
        "❤️",
        "☀️",
        "©️",
        "1️⃣",
        "#️⃣",
        "*️⃣",
        "1\u20e3",
        "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "👩\u200d",  # 流式输入尚未补齐的 ZWJ 序列也不能丢字
    ],
)
def test_italic_emoji_is_upright_and_cluster_intact(qapp: QApplication, emoji: str) -> None:
    html = render(f"_前{emoji}后_")
    assert f'<span style="font-style: normal;">{emoji}</span>' in html
    _assert_balanced(html)
    document = QTextDocument()
    document.setHtml(html)
    assert document.toPlainText() == f"前{emoji}后"
    assert document.find("前").charFormat().fontItalic()
    assert not document.find(emoji).charFormat().fontItalic()
    assert document.find("后").charFormat().fontItalic()


def test_emoji_does_not_change_links_or_bold(qapp: QApplication) -> None:
    url = "https://example.com/🙂?a=1&b=2"
    html = render(f"***[前🙂🥹后]({url})***")
    assert 'href="https://example.com/%F0%9F%99%82?a=1&amp;b=2"' in html
    document = QTextDocument()
    document.setHtml(html)
    assert document.toPlainText() == "前🙂🥹后"
    for text in ("前", "🙂", "🥹", "后"):
        fmt = document.find(text).charFormat()
        assert fmt.isAnchor()
        assert fmt.fontWeight() >= 700
        assert fmt.fontItalic() == (text in ("前", "后"))


def test_plain_symbols_keep_italics(qapp: QApplication) -> None:
    text = "中文 English 123 © ™ ↔ + = # ☀︎ 🙂︎ 𐐀"
    document = QTextDocument()
    document.setHtml(render(f"*{text}*"))
    assert document.toPlainText() == text
    for character in text:
        assert document.find(character).charFormat().fontItalic()


def test_emoji_entities_and_html_are_safe(qapp: QApplication) -> None:
    html = render('*&#x1F642; &amp; <span title="🙂">*')
    document = QTextDocument()
    document.setHtml(html)
    assert document.toPlainText() == '🙂 & <span title="🙂">'
    assert not document.find("🙂").charFormat().fontItalic()
    assert document.find("span").charFormat().fontItalic()
    _assert_balanced(html)


def test_emoji_in_italic_inline_code(qapp: QApplication) -> None:
    document = QTextDocument()
    document.setHtml(render("*`前🙂后`*"))
    assert document.toPlainText() == "前🙂后"
    assert document.find("前").charFormat().fontItalic()
    assert not document.find("🙂").charFormat().fontItalic()
    assert document.find("后").charFormat().fontItalic()


def test_emoji_in_highlighted_comment(qapp: QApplication) -> None:
    # 浅色 friendly 主题的注释有斜体，不能只处理 Markdown 的 <em> 标签。
    from limbowave.ui import theme

    old_palette = theme.current_palette().name
    try:
        theme.set_palette("light")
        html = render("```python\n# 前🙂后 & <tag>\n```")
        document = QTextDocument()
        document.setHtml(html)
        assert "# 前🙂后 & <tag>" in document.toPlainText()
        assert document.find("前").charFormat().fontItalic()
        assert not document.find("🙂").charFormat().fontItalic()
        assert document.find("后").charFormat().fontItalic()
        _assert_balanced(html)
    finally:
        theme.set_palette(old_palette)


def test_emoji_styling_preserves_supplementary_text(qapp: QApplication) -> None:
    text = "𐐀🙂𐐁👩🏽‍💻𐐂"
    document = QTextDocument()
    document.setHtml(render(f"*{text}*"))
    assert document.toPlainText() == text
    for letter in ("𐐀", "𐐁", "𐐂"):
        assert document.find(letter).charFormat().fontItalic()
    for emoji in ("🙂", "👩🏽‍💻"):
        assert not document.find(emoji).charFormat().fontItalic()


def test_emoji_in_attributes_is_not_rewritten() -> None:
    html = render('[文字](https://example.com "🙂")\n\n![图片](image.png "🥹")')
    assert 'title="🙂"' in html
    assert 'title="🥹"' in html
    assert 'alt="图片"' in html
    assert "<span" not in html
    _assert_balanced(html)


def test_markdown_dependencies_load_only_when_needed():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; "
            "from limbowave.ui.markdown_render import render; "
            "assert render('  '); "
            "assert 'markdown_it' not in sys.modules; "
            "assert 'pygments' not in sys.modules; "
            "assert '<strong>' in render('**hello**'); "
            "assert 'markdown_it' in sys.modules; "
            "assert 'pygments' not in sys.modules; "
            "assert '<span style=' in render('```python\\nprint(1)\\n```')"
        )], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
