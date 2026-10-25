# Page transition DPI geometry implementation plan

**Goal:** Keep workspace/settings transition snapshots aligned with live pages at non-default window sizes and 100%, 125%, 150%, 175% and 200% display scales.

**Architecture:** Separate bounded snapshot raster resolution from the logical destination rectangle. Capture with an explicit per-axis transform based on the rounded raster dimensions, and composite the complete physical source rectangle into the overlay's logical rectangle. Preserve the existing animation curves, pixel budget, live login page and interrupt behavior.

**Tech Stack:** Python, PySide6, pytest-qt, offscreen Qt scale-factor subprocesses.

## Alternatives

- Full-resolution widget grabs avoid artificial snapshot DPR but lose the existing memory/render budget at large sizes.
- Only adjusting DPR metadata still leaves rounded raster dimensions in charge of logical geometry.
- Explicit capture/destination transforms preserve the budget and make geometry independent of pixel density (selected).

## Steps

1. Add patterned/corner-marked regressions in `tests/ui/test_page_transition.py`, covering rounded snapshot dimensions, both layered endpoints, legacy/login overlays and interrupted navigation. Run the new regressions against the old implementation first.
2. Update only snapshot capture and snapshot painting in `src/limbowave/ui/main_window.py`; leave unrelated workspace changes intact.
3. Correct existing transition test coordinate sampling to account for image DPR.
4. Run the transition suite in fresh offscreen processes at scale factors 1, 1.25, 1.5, 1.75 and 2; run adjacent settings/main-window tests, lint and type checking.
5. Record measured results and limitations here. Do not commit unrelated work or claim physical multi-monitor validation from offscreen tests.

## Validation results

- Before the fix, 9 of the 12 new patterned endpoint cases failed at 150%: rounded snapshot dimensions left background/transparent pixels along the bottom or right edge at 1280x800, 1537x947 and 2003x1201. The same coverage includes an uncapped smaller snapshot at 913x617.
- Corrected the existing reciprocal-motion assertions to compare logical coordinates rather than physical image pixels; the previous assertions failed under 125% regardless of the product fix.
- Final transition suite: **37 passed at each of 100%, 125%, 150%, 175% and 200%**, in separate Qt processes (185 executions). Includes resized settings entry/exit, odd-sized interrupted/reversed navigation, pixel budget, stationary live geometry, input dismissal and live login behavior.
- Related suite at 125% (`test_page_transition`, `test_main_window`, `test_settings_panel`, `test_settings_dialog`, `test_animated_stack`, `test_login_page`, `test_login_outline_logo`): **210 passed**.
- `ruff check src/limbowave/ui/main_window.py tests/ui/test_page_transition.py`: passed. `mypy src/limbowave/ui/main_window.py`: passed. Scoped `git diff --check`: passed.
- Visually compared a real SettingsPage transition endpoint against its live page at 125% and 1537x947: layout and content geometry match. Offscreen Qt lacks the CJK fonts here, so this is geometry inspection, not typography validation. Physical multi-monitor behavior still needs an on-device check.
- Existing unrelated edits in the shared worktree were preserved; no commit was created.
