"""Code blocks must soft-wrap in Qt without mutating their source text."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QTextBrowser
from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import ChatView
from limbowave.ui.markdown_render import render


@pytest.mark.parametrize("kind", ["plain", "python", "unknown", "indented", "nested", "unclosed"])
@pytest.mark.parametrize("line", [
    "这是一条用于测试全局记忆 update_memory 弹窗审批流程的更新内容。" * 8,
    "continuous_identifier_" * 40,
    '    value = "<tag> & 🙂 ' + "long text " * 40 + '"',
], ids=["cjk", "continuous-token", "escaped-indented"])
def test_code_blocks_wrap_in_qt(qtbot: QtBot, kind: str, line: str) -> None:
    code = f"{line}\n\n    indented = 1\n\ttabbed = 2"
    if kind == "indented":
        source = "\n".join("    " + part for part in code.split("\n"))
    elif kind == "nested":
        source = "- 提交内容：\n\n" + "\n".join(
            "  " + part for part in f"```text\n{code}\n```".split("\n")
        )
    else:
        language = {"plain": "", "python": "python", "unknown": "no-such-lang"}.get(
            kind, "text"
        )
        source = f"```{language}\n{code}\n" + ("" if kind == "unclosed" else "```")

    browser = QTextBrowser()
    qtbot.addWidget(browser)
    browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    browser.setHtml(render(source))
    browser.show()
    for width in (520, 280, 520):
        browser.resize(width, 1000)
        qtbot.waitUntil(lambda w=width: browser.viewport().width() == w - 2 * browser.frameWidth())
        document = browser.document()
        assert document.size().width() <= browser.viewport().width() + 1
        block = document.find(line).block()
        assert block.isValid()
        assert not block.blockFormat().nonBreakableLines()
        assert block.layout().lineCount() > 1
        for index in range(block.layout().lineCount()):
            visual_line = block.layout().lineAt(index)
            assert visual_line.naturalTextWidth() <= visual_line.width() + 1
        # Soft wraps must not insert newlines or zero-width characters into copied code.
        assert code in document.toPlainText()


@pytest.mark.parametrize("from_history", [False, True])
def test_chat_code_wraps_after_finalize_and_resize(qtbot: QtBot, from_history: bool) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1100, 800)
    view.show()
    line = "这是测试更新内容 update_memory_" * 30
    source = f"1. 提交内容：\n\n   ```text\n   {line}\n   ```\n\n2. 执行结果：完成。"
    if from_history:
        view.load_history([("assistant", source, "", "m1")])
    else:
        view.begin_assistant()
        view.append_assistant_delta(source)
        view.end_assistant(source)
    row = view._rows[-1]
    browser = row.label
    assert isinstance(browser, QTextBrowser)
    for width in (1100, 600, 1100):
        view.resize(width, 800)
        qtbot.waitUntil(lambda: browser.document().size().width() <= browser.viewport().width() + 1)
        qtbot.waitUntil(lambda: browser.height() >= browser.document().size().height())
        block = browser.document().find(line).block()
        assert block.layout().lineCount() > 1
        assert line in browser.toPlainText()
        assert browser.horizontalScrollBar().maximum() == 0
    assert row._raw_text == source
    assert row._copy_btn is not None
    try:
        row._copy_btn.click()
        assert QGuiApplication.clipboard().text() == source
    finally:
        QGuiApplication.clipboard().clear()
