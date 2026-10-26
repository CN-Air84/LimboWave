# Code Block Soft Wrapping Implementation Plan

**Goal:** Keep desktop Markdown code blocks inside the message width without changing copied source.

**Architecture:** Keep the existing Qt rich-text table background and Pygments spans. Set `white-space: pre-wrap` on the generated `pre` element so the existing text browser wrap mode can wrap long lines while preserving indentation and explicit line breaks. Do not enable horizontal scrolling or insert characters into source text.

**Tech Stack:** Python, PySide6, markdown-it-py, Pygments, pytest-qt.

## Steps
1. Add regression tests in `tests/ui/test_code_block_wrapping.py` for fenced, highlighted, unknown-language, indented, nested-list and incomplete code blocks. Check real Qt layout at narrow widths, continuous tokens, whitespace preservation, resizing and original Markdown copying.
2. Run the new tests before the fix to reproduce clipping. Modify only the generated `pre` style in `src/limbowave/ui/markdown_render.py`.
3. Run wrapping, Markdown, chat, streaming and history regression tests; lint changed Python files. Render a local screenshot for visual verification. Preserve all unrelated in-progress changes; do not commit them.
