# Settings About Tab Implementation Plan

**Goal:** Add a rich, clearly marked placeholder About tab to settings without changing configuration or performing network requests.

**Architecture:** A reusable, read-only PySide6 AboutPage is shared by the full-window settings page and the legacy settings dialog. Section definitions are kept together for later copy replacement. Existing theme tokens and the existing tab animation are reused.

**Tech Stack:** Python 3.12+, PySide6, pytest-qt, ruff, mypy.

## Design

- Append 关于 after existing categories; preserve category indices and the default tab.
- Use a restrained brand header and vertically stacked cards in a keyboard-scrollable QScrollArea.
- Include application, releases, team, resources/community, support, credits, privacy/data, legal, roadmap, contribution, and additional resources placeholders.
- Every unknown fact is explicitly 待填写; unimplemented actions are disabled and marked 待接入.
- No invented version, copyright holder, license, URLs, privacy guarantees, or network behavior.
- Inherit the application's font; use current color, radius and font-scale tokens. Allow labels to wrap at narrow widths.
- Keep the page stateless and available without optional services, including vault and preferences.

## Task 1: Specify behavior in tests

Create tests/ui/test_about_page.py: verify all sections, disabled actions, plain-text/selectable copy, scrolling at compact sizes, and light/dark/font-scale restyling.
Extend tests/ui/test_settings_panel.py: update category expectation and verify click/programmatic navigation, reopening, and independence from services.
Extend tests/ui/test_settings_dialog.py: verify the shared page is available in the legacy dialog.
Run targeted pytest; the new page imports should fail before implementation.

## Task 2: Implement the About page

Create src/limbowave/ui/about_page.py with declarative section content, reusable labels/cards, placeholder actions and restyle().
Modify src/limbowave/ui/settings_panel.py to append the new page and forward restyle().
Modify src/limbowave/ui/settings_dialog.py to append the shared page.
Do not touch existing user changes outside these integration points. Do not commit unrelated work.

## Task 3: Verify

Run targeted tests and existing settings tests, ruff and mypy for affected source files.
Run the full UI test suite when feasible; distinguish pre-existing failures from regressions.
Render the actual QWidget offscreen at desktop and compact sizes, inspect screenshots and fix wrapping/scroll issues.

## Verification results

- Implemented 11 sections, 47 placeholder fields and 8 explicitly disabled future actions.
- All 94 About/settings tests pass, including navigation, reopen, theme refresh and 360px-wide layouts up to 160% font scale.
- Ruff passes for all six affected Python files; mypy passes for the three affected source files.
- Rendered and visually inspected light/dark, footer and compact views with installed Microsoft YaHei fonts loaded for offscreen rendering.
- Full UI run stopped at an unrelated existing checkbox pixel assertion: 112 passed, 1 skipped, 1 failed. The exact unchanged checkbox test also fails in isolation under offscreen Qt, without importing the About page. Full UI success is not claimed.
