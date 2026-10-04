# Message Send Animation Implementation Plan

**Goal:** Give newly accepted user messages a short upward fade-in without delaying sending or streaming.

**Architecture:** Animate a temporary paint-only Qt graphics effect on the new user row. Keep layout geometry unchanged so composer docking, wrapping, attachments and scroll following remain authoritative. History, assistant streaming and existing retry transitions retain their current behavior.

**Tech Stack:** PySide6 QGraphicsEffect / QPropertyAnimation, pytest-qt.

## Design

- Prefer a restrained 260 ms, 14 logical-pixel upward fade with OutCubic easing over a plain fade or a large composer-to-transcript flight.
- Trigger only from add_user_message after the application accepts a user message; button and Ctrl+Enter share this path.
- Include attachments in the row animation. Do not animate loaded history or every assistant delta.
- Skip animation when hidden or Qt's general UI effects are disabled.
- Remove the effect on completion; cancel on hide, row reset and conversation replacement. Never block network work or retain stale row callbacks.

## Tasks

1. Add focused tests in tests/ui/test_message_send_motion.py for live sends, history exclusion, motion-disabled/hidden states, geometry, independent animations and cleanup. Run them to confirm failure first.
2. Add src/limbowave/ui/message_motion.py with the paint-only fade/translation effect. Wire its lifecycle into _BubbleRow and ChatView.add_user_message in src/limbowave/ui/chat_view.py.
3. Run the new tests and existing chat/retry/history tests, lint and type checks. Inspect rendered light/dark frames, including wrapped text and attachments.

## Verification

- `.venv/Scripts/python.exe -m pytest tests/ui/test_message_send_motion.py tests/ui/test_chat_view.py`
- `.venv/Scripts/python.exe -m ruff check src/limbowave/ui/message_motion.py src/limbowave/ui/chat_view.py tests/ui/test_message_send_motion.py`
- `.venv/Scripts/python.exe -m mypy src/limbowave/ui/message_motion.py src/limbowave/ui/chat_view.py`

Preserve all pre-existing workspace changes; no unrelated refactors or commits.

## Results

- Native Windows run: 85 chat/send-animation tests passed with exit code 0; the known environment-sensitive hover test was deselected. Coverage includes input methods, streaming, history, scroll geometry and interrupted transitions.
- All 14 new tests also passed at 150% UI scale; the pixel-level test verifies both opacity and translation past the resting widget bounds.
- Ruff passed for all changed Python files; the new effect module passed strict mypy.
- Existing chat_view.py still has an unrelated QImage.save(QBuffer, str) stub mismatch (line 1405 after this change). No unrelated fix applied.
- The native Windows hover test timed out both with and without the new animation. Offscreen, all 86 tests passed their assertions, but Qt crashed at process shutdown (0xC0000005). Running the 72 pre-existing chat tests with the new animation disabled reproduced that shutdown crash; this is not a clean offscreen-suite pass.
- Inspected light/dark Qt-rendered frames with narrow layout, wrapped Chinese text and image/document attachments. Local previews are in ignored dist/message-send-animation/.
