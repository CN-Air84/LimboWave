"""Read-only text extraction: no scripts, subresources, forms, or browser state."""

from __future__ import annotations

import re
from email.message import Message
from html.parser import HTMLParser
from typing import Any

from limbowave.domain.network import WebAccessError, WebResponse

_SKIP = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "form",
    "nav",
    "footer",
    "aside",
    "button",
    "input",
    "select",
    "textarea",
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BREAK = {"p", "div", "article", "main", "section", "h1", "h2", "h3", "h4", "li", "tr", "br"}


def clean_text(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = "hidden" in attributes or (attributes.get("aria-hidden") or "").lower() == "true"
        style = (attributes.get("style") or "").replace(" ", "").lower()
        hidden = hidden or "display:none" in style or "visibility:hidden" in style
        suppressed = bool(self._stack and self._stack[-1][1]) or tag in _SKIP or hidden
        if tag in _BREAK and not suppressed:
            self.parts.append("\n")
        if tag not in _VOID:
            self._stack.append((tag, suppressed))
        if len(self._stack) > 2048:
            raise WebAccessError("invalid_content", "网页嵌套层级超过抽取上限")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break
        if tag in _BREAK and not (self._stack and self._stack[-1][1]):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][1]:
            return
        if any(tag == "title" for tag, _ in self._stack):
            self.title.append(data)
        elif not any(tag == "head" for tag, _ in self._stack):
            self.parts.append(data)


def extract_page(
    response: WebResponse, *, max_bytes: int = 48 * 1024
) -> tuple[dict[str, Any], bool]:
    header = Message()
    header["content-type"] = response.content_type
    charset = header.get_content_charset()
    if not charset and "html" in response.content_type.lower():
        match = re.search(rb"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", response.body[:4096], re.I)
        charset = match.group(1).decode("ascii") if match else None
    try:
        decoded = response.body.decode(charset or "utf-8-sig", errors="replace")
    except (LookupError, ValueError):
        decoded = response.body.decode("utf-8", errors="replace")
    title = ""
    if "html" in response.content_type.lower():
        parser = _ArticleParser()
        parser.feed(decoded)
        parser.close()
        title = " ".join(clean_text("".join(parser.title)).split())[:512]
        lines = [" ".join(line.split()) for line in clean_text("".join(parser.parts)).splitlines()]
        text = "\n".join(line for line in lines if line)
    else:
        text = clean_text(decoded).strip()
    if not text:
        raise WebAccessError(
            "empty_content", "页面没有可抽取正文（可能需要 JavaScript、登录或订阅）；未阅读全文"
        )
    encoded = text.encode("utf-8")
    truncated = response.truncated or len(encoded) > max_bytes
    return {
        "url": response.url,
        "title": title,
        "status": response.status,
        "content_type": response.content_type,
        "bytes": len(response.body),
        "text": encoded[:max_bytes].decode("utf-8", errors="ignore"),
        "retrieved_at": response.retrieved_at,
        "evidence_type": "page_content",
        "trust": "untrusted_external_content",
        "truncated": truncated,
    }, truncated
