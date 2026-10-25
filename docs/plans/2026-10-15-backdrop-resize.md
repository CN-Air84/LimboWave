# Backdrop resize responsiveness

**Goal:** Keep resizing responsive with wallpaper and blur enabled.

**Architecture:** Keep cached frames visible during resize, debounce new renders, and invalidate obsolete results immediately. Separate frame repainting from material/style installation so resizing does not restyle the conversation subtree. Keep the existing bounded worker and cache.

**Tech Stack:** Python, PySide6, Pillow, pytest-qt.

1. Add regressions in `tests/ui/test_backdrop_resize.py` for resize restyling, stale results during debounce, cache-hit races and completed-but-undelivered duplicate work. Run against current code.
2. Update `src/limbowave/ui/backdrop.py` to invalidate superseded jobs on resize/cache hits, deduplicate queued deliveries, and remove unnecessary GUI image copies.
3. Update `src/limbowave/ui/main_window.py` to repaint unchanged material state without stylesheet reinstallation; retain theme/history-preview updates.
4. Run focused appearance/popup/window tests, lint and typing; document measured results in `docs/development/appearance.md`.

Alternatives considered: increasing debounce alone leaves expensive style refreshes intact; reducing render resolution changes wallpaper quality. Preserve visual quality and remove unnecessary GUI work first.

## Validation

- All four original regressions failed before the fix: 3486 style changes on resize, stale cache overwrite, duplicate render while GUI delivery was pending, and stale-size installation during debounce.
- All seven final resize/render regressions pass, including cooperative cancellation, retry after failure and pixel checks on sidebar/chat/composer.
- Appearance suite: 19 passed, 1 skipped (offscreen platform has no fonts).
- Broader resize/popup/main-window/page-transition run: 91 passed, 1 failed, 1 teardown error. The popup theme assertion and missing popup slot, plus a separate appearance-editor callback after deletion when suites are combined, also reproduce using the unmodified HEAD versions of both edited source modules. These existing failures are outside this fix.
- `ruff check .` passes; `mypy src` passes for all 216 source files.
- Isolated offscreen GUI callback benchmark at 1920x1080 with 20 messages and a history preview (10 samples): median 135.956 ms before / 0.048 ms after, max 191.994 / 0.297 ms. This measures material refresh callbacks, not native Windows resize frame rate.
