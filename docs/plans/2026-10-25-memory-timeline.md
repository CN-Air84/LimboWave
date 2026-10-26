# Footprint & Timeline View Implementation Plan

**Goal:** Give retained memories a calm, chronological desktop gallery with honest local-encryption messaging.

**Architecture:** Add a read-only projection over current encrypted memory documents, not a second archive. Deduplicate unchanged fork copies, retain distinct edited versions, resolve source conversations where available, and preserve creation/source metadata on future edits. Reuse the desktop full-page navigation and background task runner. No network requests, plaintext caches, schema migration, or change to model memory injection.

**Tech Stack:** Python 3.12+, PySide6, existing SQLite/VaultKey repository, pytest/pytest-qt.

## Design decisions
- Dedicated sidebar “足迹” entry, full-page gallery, generous spacing, date rail, quiet cards, existing light/dark/font tokens.
- Newest/oldest order; local-calendar day groups; text, scope, source and month filters; incremental card rendering; expandable complete plain text; source navigation and memory management.
- Badge states that memory bodies are encrypted locally. Expanded explanation distinguishes local browsing from provider requests and user-created backups. Never promise device-exclusive content while memory injection remains enabled.
- Only current global/branch memories are eligible. Snapshots, receipts, policies and overrides are not resurfaced. Deleted memories disappear; unchanged fork copies count once. Global and branch copies remain distinct.
- Historical dates are not invented. Invalid timestamps are shown as unknown; legacy naive timestamps are interpreted as UTC. Existing versions overwritten before this change cannot be reconstructed.

## Task 1: Projection and metadata
Files: `src/limbowave/domain/footprint.py`, `src/limbowave/domain/memory.py`, `src/limbowave/application/services/memory_timeline.py`, `src/limbowave/application/services/memory_service.py`, `src/limbowave/application/repositories.py`, both memory repositories, `tests/unit/test_memory_timeline.py`.
1. Add failing double-backend tests for current-only reads, ordering, deduplication, deletion, retained timestamps/source, source resolution, old JSON and encrypted storage.
2. Add `list_current()` to both repositories and port. Select only exact current-list IDs, avoiding decryption of snapshots.
3. Add immutable presentation records and a pure timestamp parser. Project inside one UoW, validate live locations, and cache metadata reads.
4. Preserve `created_at` and `source` on edits; add optional `updated_at` in the existing encrypted payload.
5. Run `.venv/Scripts/python.exe -m pytest tests/unit/test_memory_timeline.py tests/unit/test_memory_service.py`.

## Task 2: Gallery and navigation
Files: `src/limbowave/ui/memory_timeline.py`, `src/limbowave/ui/sidebar.py`, `src/limbowave/ui/theme.py`, `src/limbowave/app.py`, `tests/ui/test_memory_timeline_page.py`, `tests/ui/test_footprint_navigation.py`.
1. Add tests for loading/empty/error/retry, filters/order/date groups, full text, source signals, pagination, local privacy copy and late async results.
2. Build the read-only gallery with semantic accessible labels, keyboard controls, theme tokens and no HTML interpretation of stored text.
3. Connect sidebar entry, workspace return, settings-memory entry and existing source-navigation handler. Refresh on open/manual refresh and completed model runs; tear down subscriptions on close.
4. Run focused UI and existing navigation/memory regression tests.

## Task 3: Verification and documentation
Files: `docs/development/memory.md`.
1. Document entry point, date semantics, deduplication/deletion and privacy boundary.
2. Run Ruff, mypy, relevant unit/UI suites; report unrelated baseline failures separately.
3. Capture synthetic-data light/dark gallery screenshots and inspect layout at normal/narrow sizes, without touching the user's actual vault.

No commits or parallel agents are required for this in-place implementation.

## Implementation verification (2026-10-26, client local date)

- Implemented the projection, timestamp/source preservation, gallery, sidebar/full-page navigation, source-message focus and privacy explanation. No new database migration or network endpoint.
- Successful completed test runs: 167 memory-service/runtime/preset/timeline/active-memory unit tests; 53 gallery/navigation/sidebar/main-window UI tests; 6 source-focus/search UI tests; 4 new-session memory tests. The source-navigation test additionally exercised an actual retained source message through the wired application.
- Ruff passed for feature modules and touched integration files. Mypy passed for 10 feature/integration modules (`--follow-imports=silent`).
- Synthetic-data screenshots inspected in dark/light, 720px narrow view and enlarged-font view; output is in the ignored `.tmp/footprint-preview/` directory. Fonts for the offscreen preview were loaded from Windows locally; no user vault data was used.
- Broader old memory-panel/popup UI regression runs encountered a Windows Qt access violation while processing events, so those suites are not claimed as fully passing. Feature-specific and the successful separate regression runs above completed normally.

## Visual correction: gallery background (2026-10-26)

The first gallery reused the app's lowest opaque canvas and did not attach to the
window's shared wallpaper engine, leaving a near-black background and opaque scroll
body. The corrected gallery paints a static palette-derived lifted slate/paper
gradient when no wallpaper is available; when there is a wallpaper it reuses the
existing cached `BackdropEngine`. Labels, scroll viewport and day groups remain
transparent, with separate readable card surfaces. No new assets, image loads or
animation loops are introduced, and scrolling does not recompose the wallpaper.

Files: `src/limbowave/ui/memory_timeline.py`, `src/limbowave/ui/theme.py`,
`src/limbowave/ui/main_window.py`, `tests/ui/test_footprint_background.py`.
Pixel tests cover the fallback gradient, transparent gaps, dark/light/custom palette
colors, live theme changes, missing assets, shared-wallpaper hookup and replacement.

Background correction verification: gallery/navigation/main-window run completed with
43 tests passing; wallpaper/background regression completed with 33 tests passing and
1 font-dependent offscreen test skipped. Ruff and targeted mypy passed. Updated
synthetic-data dark/light/narrow/large-font screenshots were inspected locally.

## Readability and scrolling title correction (2026-10-26)

The provided screenshot shows heading, filters and dates drawn directly on a bright
wallpaper. Add two theme-colored, opaque reading boards: one for navigation, title
and filters, another for date headings, cards and states. Wallpaper stays visible
around the boards, but the user's material opacity setting cannot defeat text
contrast inside them. Keep card/control surfaces separated from their board.

Group only the large introduction (eyebrow, headline and subtitle) into a collapsible
region. Collapse it with a short height animation after scrolling down; restore it
when returning to the top. Back, search, scope/source/month/order filters and privacy
controls remain visible. Guard programmatic re-rendering and animation-induced range
changes so the title does not oscillate at the bottom of short lists. Filtering back
to the beginning restores the introduction. No changes to persistence or model APIs.

Files: `src/limbowave/ui/memory_timeline.py`, `src/limbowave/ui/theme.py`,
`tests/ui/test_footprint_readability.py`, background pixel tests and memory docs.
Verification: bright/dark wallpaper contrast, filter accessibility while collapsed,
wheel/key/scrollbar navigation, top restoration, short-list stability, filtering,
narrow/enlarged text preview, and existing gallery/background/navigation suites.

## Follow-up: compact metadata and transparent memory list

Per the user's follow-up, the memory count/day count row and local-browsing notice
now belong to the animated title region, collapsing/restoring with the headline.
Privacy details remain accessible in the fixed navigation even while the notice is
collapsed. The outer list container no longer has a background plate or extra
insets; each memory card retains an opaque palette-derived surface, and the header
keeps its readability plate. No new background behind the list/date groups.

Regression tests now assert both metadata rows hide/restore, privacy details work
while collapsed, wallpaper shows through gaps in the list, and header/card text
remains readable on dark, white and blue backgrounds.

Compact-header/list-plate follow-up verified: 34 gallery/background/readability tests
passed, plus 30 navigation/source-focus/wallpaper regression tests; 1 existing
font-dependent offscreen test skipped. Ruff and targeted mypy passed. Synthetic
bright-wallpaper dark/light and narrow screenshots show the outer list plate removed
and both metadata rows hidden after scroll; compact header height reduced from 299px
to 155px in the normal-font preview.

## Return-action placement follow-up (2026-10-26)

Move the gallery's return action to the last/rightmost navigation position, matching
the settings page's flat, arrow-free return button. Keep the compact page title at
the left and privacy/manage/refresh immediately before return. Return remains visible
when the introduction collapses; Esc and draft-preserving workspace navigation are
unchanged. Tests cover normal/narrow widths, collapsed placement and actual clicking
through the wired app.

## Half-docked header follow-up (2026-10-26)

After the introduction and metadata collapse, slide the entire remaining header
upward by half of its compact height. Its upper half is clipped outside the page;
the lower half docks at the window's top edge with no original top inset. Use a
clipping host in the layout rather than floating overlays so list space is reclaimed
and hidden controls do not receive mouse input. Animate header docking and the
existing title collapse together; derive heights from the layout at the active width
and font scale, restore both on returning to the top/filter reset, and keep the
existing scroll-range guards. Esc still returns to chat. Keyboard focus/Find must
reveal controls before focusing them instead of leaving focus outside the viewport.

Files: gallery widget, targeted docking/readability tests, feature documentation.
No model, storage, or background-rendering changes.

Half-docking verified: 46 header-docking/readability/gallery/navigation/background
UI tests passed. Ruff and targeted mypy passed. Tests cover the exact negative
half-height position at 720/1080 widths, clipped hit regions, font-scale/resize
recalculation, zero top inset, full restoration, filter reset, Ctrl+F, Escape and
motion disposal. Synthetic bright-wallpaper and narrow previews were inspected.

## Regression fix: sidebar footer outside the screen after leaving footprints

The half-docking header's height-for-width calculation briefly measured wrapped text
at its placeholder width before QStackedWidget assigned the real page width. It
installed a ~1500–1700px fixed header height and caused the main window to enlarge;
returning preserved the expanded window geometry and put Footprints, Mini Companion,
Settings and the composer below the visible screen. Clamp the measurement width to
the panel layout's own minimum width, matching the geometry Qt enforces. Do not
resize/reset the user's main window as a workaround or alter sidebar visibility.

Reproduced before the fix with a fresh 1100x760 desktop window growing to 1100x1710.
After the fix the same script stays 1100x760 through open, dock and return, with all
three sidebar footer buttons inside the page and directly hit-testable. Added
normal/narrow/maximized/font-scale repeated-navigation regression cases plus wired
application assertions for footprint and settings returns.

Footer regression verification: 69 layout/header/background/gallery/main-window
regression tests passed, plus both wired-app navigation tests (the separate 8-test
run includes 6 layout cases already counted). Ruff and targeted mypy passed.
Synthetic before/after reproduction confirmed unchanged 1100x760 window geometry
and all sidebar footer buttons plus composer remain inside the visible client area.
