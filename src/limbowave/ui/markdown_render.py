"""Markdown → Qt 富文本渲染（Task 3.1：Markdown 和代码块）。

实现路径：``markdown-it-py`` 产出 HTML，``pygments`` 给代码块上语法色。
Qt 的富文本引擎只认一个 HTML 子集，因此：

- 代码高亮用 ``pygments`` 的 **内联样式**（``noclasses=True`` + ``nowrap=True``），
  只产 ``<span>`` 序列，外层 ``<pre>`` 由 markdown-it 统一生成——两套包裹
  逻辑不嵌套，结构才不会乱；
- 代码块外层包一个 ``<table>`` 充背景板——Qt 富文本对 ``<pre>`` 的背景色支持
  不稳定，对 ``<table bgcolor>`` 的支持是可靠的（Qt 文档列出的子集）；
- 链接染成强调色，``QTextBrowser`` 处理点击；
- emoji 按完整字素簇使用正体，避免 Windows 彩色字体被合成斜体后渲染损坏。

流式安全：``render`` 必须是**纯函数**——流式中途的半个代码块也要产出合法
HTML，不能抛异常。markdown-it 对未闭合围栏会等到收尾行才开块，因此流式
中途不会渲染出半个高亮块。
"""

from __future__ import annotations

from functools import lru_cache
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRegularExpression

from limbowave.ui import theme

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

# 代码高亮：内联样式（Qt 不支持 class CSS），nowrap 只产 <span>，
# 外层 <pre> 由 markdown-it 统一包裹。
# 配色**跟随主题**——深色用 monokai、浅色用 friendly，
# 否则浅底上会出现浅色文字（不可读）。formatter 按需构造并缓存。
# HtmlFormatter 不是泛型类；用 dict[str, object] 存并就地取回
_formatter_cache: dict[str, Any] = {}


def _formatter() -> Any:
    style = theme.pygments_style()
    if style not in _formatter_cache:
        from pygments.formatters import HtmlFormatter

        _formatter_cache[style] = HtmlFormatter(noclasses=True, nowrap=True, style=style)
    return _formatter_cache[style]


def _highlight(code: str, lang: str | None, _attrs: str | None) -> str:
    """markdown-it 的 highlight 回调。未知语言回退纯文本（转义）。"""
    if lang:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = None
    else:
        lexer = None
    if lexer is None:
        return code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    from pygments import highlight

    rendered: str = highlight(code, lexer, _formatter())
    return rendered.rstrip("\n")


@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    # Login/empty conversations need neither the parser nor the lexer registry.
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"highlight": _highlight, "html": False, "breaks": False})
    parser.enable("table")
    parser.enable("strikethrough")
    return parser

# Qt 的 Unicode 属性识别 emoji，\X 一次匹配完整字素簇（肤色、ZWJ、旗帜等）。
# 普通数字 / © / ™ 等文本符号不匹配，除非明确使用 emoji 选择符或组成键帽。
_EMOJI = QRegularExpression(
    r"(?=\p{Emoji_Presentation}(?!\x{FE0E})"
    r"|\p{Extended_Pictographic}(?:\x{FE0F}|\x{200D}|\p{Emoji_Modifier})"
    r"|[#*0-9]\x{FE0F}?\x{20E3})\X"
)


class _EmojiStyler(HTMLParser):
    """只处理生成 HTML 的文本节点，绝不把 span 写进 href / title 等属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_data(self, data: str) -> None:
        matches = _EMOJI.globalMatch(data)
        if not matches.hasNext():
            self.parts.append(data)
            return
        # Qt 的匹配偏移是 UTF-16 单元，不是 Python 字符索引；补充平面的 emoji
        # 占两个单元，直接切 Python 字符串会丢掉或重复周围文字。
        encoded = data.encode("utf-16-le")
        end = 0
        while matches.hasNext():
            match = matches.next()
            start = match.capturedStart() * 2
            self.parts.append(encoded[end:start].decode("utf-16-le"))
            self.parts.append(f'<span style="font-style: normal;">{match.captured()}</span>')
            end = match.capturedEnd() * 2
        self.parts.append(encoded[end:].decode("utf-16-le"))


# 代码块背景由 theme.CODE_BG 提供（跟随主题）


def render(text: str) -> str:
    """把 Markdown 渲染成 Qt 富文本 HTML。纯函数，绝不抛。"""
    try:
        body = _markdown().render(text) if text.strip() else ""
    except Exception:
        # 渲染器本身出问题也不让消息消失——回退转义纯文本
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<p>{escaped}</p>"
    body = _restyle(body)
    return f'<div style="color: {theme.TEXT_PRIMARY};">{body}</div>'


def _restyle(html: str) -> str:
    """给渲染产物补主题相关的内联样式（代码块背景、引用条、表格边框）。"""
    # 代码块背景板：markdown-it 产出的 <pre> 只来自围栏代码块
    html = html.replace(
        "<pre>",
        f'<table width="100%" cellpadding="8" cellspacing="0" bgcolor="{theme.CODE_BG}">'
        f'<tr><td><pre style="font-family: {theme.FONT_MONO};">',
    )
    html = html.replace("</pre>", "</pre></td></tr></table>")
    # 引用块
    html = html.replace(
        "<blockquote>",
        f'<blockquote style="border-left: 3px solid {theme.ACCENT};'
        f' color: {theme.TEXT_SECONDARY}; margin-left: 0; padding-left: 12px;">',
    )
    # 行内代码
    html = html.replace(
        "<code>",
        f'<code style="font-family: {theme.FONT_MONO}; background: {theme.CODE_BG};'
        f' padding: 1px 4px; border-radius: 4px;">',
    )
    # 链接
    html = html.replace("<a href", f'<a style="color: {theme.ACCENT};" href')
    # 表格边框
    html = html.replace(
        "<table>",
        f'<table border="0" cellpadding="6" cellspacing="0"'
        f' style="border: 1px solid {theme.BORDER};">',
    )
    # 放在高亮 / 主题处理之后，代码注释里的斜体也得到同样的修复。
    if not _EMOJI.match(html).hasMatch():
        return html
    styler = _EmojiStyler()
    styler.feed(html)
    styler.close()
    return "".join(styler.parts)
