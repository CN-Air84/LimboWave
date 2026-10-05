# Two-level Logical Model Selector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the composer's flat upward model list with brand → logical display name menus and per-model right-click site selection.

**Architecture:** Keep the QComboBox selection API and stable logical IDs, but render a cascading QMenu with shared popup material/motion. Pure keyword classification uses logical ID, display name, then bound remote IDs; unknowns remain available under 其他. Send a single (logical model, endpoint) request to the existing guarded runtime transition.

**Tech Stack:** Python 3.12+, PySide6, pytest/pytest-qt.

---

## Design decisions
- Prefer native cascading menus over two independent selectors (extra persistent UI) or a custom two-column popup (unnecessary input/focus handling).
- Order nonempty groups: Deepseek, Qwen, Kimi, GLM, MiniMax, Mimo, StepFun, SenseNova, Claude, Gemini, GPT, Grok, 其他. Preserve configuration order within groups.
- Match case-insensitively; step-prefixed remote IDs belong to StepFun. A recognizable logical ID takes precedence over the display name and remote bindings.
- Left-click selects the logical model using its configured default as before. Right-click or Menu/Shift+F10 on a model lists only its bound sites, using endpoint display names and marking default/current. Selecting another model's site must not first route through its default.
- Preserve advanced-toolbar site switching, current routing/capability/thinking updates, busy guard, error rollback, and no automatic resend.
- Unbound models remain visible; their context menu shows a disabled explanation. Refresh/selection/dismissal must close stale menus. Reuse theme materials; allow keyboard navigation and screen-edge placement.

### Task 1: Classification (test-first)
Create src/limbowave/domain/model_brand.py and tests/unit/test_model_brand.py. Cover all brands, mixed case, namespace prefixes, step IDs, generic aliases backed by remote IDs, unknown fallback, and false positives. Run .venv/Scripts/python.exe -m pytest tests/unit/test_model_brand.py -q (red then green).

### Task 2: Selector UI (test-first)
Create src/limbowave/ui/model_selector.py and tests/ui/test_model_selector.py. Modify only the selector construction/API in src/limbowave/ui/chat_view.py. Keep addItem/currentData/currentText compatibility; render brand/model menus with checks, popup material, upward root placement, right-click/keyboard site menu, and deterministic closing. Test grouped ordering, real clicks, signal payloads, empty states, refresh, escaping labels, and dismissal.

### Task 3: Routing integration (test-first)
Update src/limbowave/app.py to populate site metadata for every logical model and extend _apply_logical_model with an optional endpoint target. Connect one combined model/site signal; clear override for default selection and preserve existing no-op behavior for ordinary current-model clicks. Add callback-level tests in tests/ui/test_model_selector_routing.py for target binding, current-model site changes, restore default, busy/error guards, and toolbar metadata.

### Task 4: Verification
Run new tests, existing chat/toolbar/popup/routing tests, then the full suite if practical. Run Ruff on new files and changed-line checks on premodified files; inspect a rendered selector screenshot if supported. Review the task's incremental diff against the pre-existing working changes. Do not commit or reset unrelated work.

## Implementation notes and verification
- Implemented classification, cascading model menus, model-specific site metadata, and atomic model/site routing. Kept the original QComboBox ID/display-name API and advanced-toolbar entry point.
- The workspace's concurrent binding-priority update removed default_binding; menu defaults now follow bindings[0], consistent with RoutingService.
- Preserved the shared combo-frame animation with a comboPopupOpen property in theme_effects.py. Added regression coverage for outside-click dismissal, keyboard navigation, repeated reopening, long lists, literal ampersands/duplicate names, and theme open-state feedback.
- Final focused suite: 239 passed (classification, selector, app callbacks, existing chat/toolbar/popup/theme/routing tests).
- Ruff passed for new modules/tests and theme_effects.py; focused mypy passed for the two new source modules.
- Dark/light popup layouts were rendered with local system fonts and visually inspected. Rendering artifacts are temporary test output, not repository assets.
- The first combined unit/UI collection hit an existing duplicate basename, test_compression_branch_restart.py; the wider retry uses --import-mode=importlib without deleting or renaming unrelated tests.
- Wider unit/UI suite (--import-mode=importlib): 2289 passed, 7 skipped, 9 failed. Isolated rerun of all nine: 6 passed; 3 reproducible failures (shell child output, checkbox pixels, diagnostics log lock). The same nine tests run against the pre-edit app/chat/theme source snapshots through a temporary import loader produced the identical 6 passed / 3 failed result. No workspace sources were rolled back for this comparison.
