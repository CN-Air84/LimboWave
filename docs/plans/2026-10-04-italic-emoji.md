# Italic Emoji Rendering Implementation Plan

**Goal:** Preserve Markdown italics for text while rendering emoji upright in Qt rich text.

**Architecture:** Post-process generated HTML text nodes only. Use Qt Unicode emoji properties and grapheme matching rather than maintaining Unicode ranges; keep complete emoji sequences in a single `font-style: normal` span. Do not change link attributes, text content, font selection, or surrounding emphasis.

**Tech Stack:** Python, markdown-it-py, PySide6, HTMLParser, pytest-qt.

## Steps
1. Add failing regressions in `tests/ui/test_markdown_render.py`: Qt character formatting, complete emoji clusters, nested emphasis, links, highlighted code, entities, and partial streaming input.
2. Update `src/limbowave/ui/markdown_render.py` with text-node-only emoji styling. Preserve generated markup and existing fallback behavior.
3. Run renderer and chat UI regressions, Ruff and mypy checks; render an offscreen before/after comparison on Windows and inspect it.

## Scope
Keep existing user changes intact. No dependency additions, global italic removal, forced emoji font, or changes to chat lifecycle.

## Verification results
- Windows native Qt renderer: 32 tests passed, exit code 0.
- Ruff lint/format and focused mypy checks passed.
- Native Windows font before/after rendering reproduced the reported distorted emoji and verified upright emoji with surrounding italics preserved (`.var/probes/italic-emoji-comparison.png`).
- Expanded offscreen Markdown/chat/message-list suite: 114 assertions/tests passed, followed by an access violation during Qt process shutdown (exit -1073741819). The same shutdown failure reproduced with emoji styling disabled (82 chat/message-list tests passed); left this unrelated UI teardown issue unchanged.
