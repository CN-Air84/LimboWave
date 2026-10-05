# Retry Without Transient Bubbles Implementation Plan

**Goal:** Every retry updates the original visible turn, including intermediate failures, repeated events and confirmed endpoint failover.

**Architecture:** Reuse the assistant card status label for run errors and retain failed empty cards. Retry resets that same widget. Ignore stale/missing-source retry events instead of converting them to sends. Confirmed failover uses the captured user message ID through retry_user_message, never send(text).

**Tech Stack:** Python, PySide6, pytest-qt, pytest-asyncio.

1. Add regressions in tests/ui/test_retry_ui.py for row identity immediately on errors, empty failed attempts, stale retry replay and missing origins; add failover callback tests. Run them before production changes.
2. Update src/limbowave/ui/chat_view.py to keep run errors inside the card and clear them in reset_for_retry, retain failed cards at settlement, and reject unmatched retry events.
3. Update src/limbowave/app.py so confirmed failover retries the captured source only when its session/branch still matches and endpoint switching succeeds. Preserve attachments via the existing retry path.
4. Run focused retry/chat/routing tests, broader UI and unit regressions, Ruff and mypy. Keep all unrelated workspace changes intact.

## Verification

- Six new row-identity/error-timing regressions failed before the UI fix and passed afterward.
- Focused retry, failover, chat view, retry rules and coordinator tests: **223 passed**.
- Ruff: passed. Mypy: passed for both production files.
- Offscreen visual QA captured failed/retrying/succeeded states; the original user and assistant widgets remain identical throughout. Images: `.var/retry-no-transient/retry-sequence.png`.
- Wider UI checks were not green. The routing fixture has four failures from a missing `_clear_thinking_trial` stub; AST comparison confirms `_apply_logical_model` is unchanged by this patch. The full UI attempt also reported other failures and exited without a complete captured summary; it is not counted as a passing run.
- The running desktop process uses source code loaded before these edits. Restart is required to load the fix; the user's application was not stopped.
