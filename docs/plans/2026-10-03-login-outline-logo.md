# Login Outline Logo Implementation Plan

**Goal:** Place the supplied geometric two-line Logo in the login page's upper-right area, with transparent letter interiors and a one-shot outline drawing animation.

**Architecture:** Preserve the original branding assets and add an outlined SVG in the packaged UI assets. A small, mouse-transparent Qt widget animates each contour using SVG dash offsets, reusing theme colors and the existing login lifecycle. No dependencies or changes to the password flow.

**Tech Stack:** PySide6 / QSvgRenderer / QPropertyAnimation, pytest-qt.

## Design

- Use the new geometric wordmark, not the older handwritten asset.
- Keep both outside edges and counters, with no background or solid lettering.
- Upper-right, about 40% of window width (maximum 540 logical pixels), proportional at compact sizes.
- Stagger contours over 2.6 seconds, then stay still; do not replay on password retries.
- Keep the existing heading and form choreography. Decorative widget must not intercept focus or clicks.
- Settle before transition snapshots and when hidden; paint in current light/dark theme colors.

## Tasks

1. Add tests for transparent outlines, progressive strokes, layout, animation lifecycle and snapshot completion. Run to confirm failure before implementation.
2. Add packaged outline asset and OutlineLogo widget; integrate only the logo into LoginPage.
3. Run targeted login/UI tests, lint/type checks, and inspect actual Qt renders at large/compact sizes in light and dark themes.

## Revised requirement: submission-driven loading transition

- Idle login shows a static outline. Return / green arrow starts a draw → hold while needed → erase sequence; duplicate submits are disabled.
- Password work runs via the existing startup worker helper so the GUI remains responsive.
- MainWindow keeps the login page current until both startup handoff and the complete contour erase have finished. No login/workspace slide or crossfade.
- Use a clamped frame clock, not wall-clock animation jumps, so synchronous startup stalls cannot skip drawing/erasure frames.
- Password retries and confirmation prompts wait for the current sequence to finish, then restore the form and static logo. Escape cannot bypass an in-progress submission; closing the window remains possible.
- Verify fast/slow readiness, keyboard/button parity, retry, duplicate submission, event-loop stall, full-path disappearance, and unchanged non-login transitions.

## Final verification

- Implemented the revised submission-driven behavior, superseding the original on-show animation plan above.
- Static outline while idle; submission hides the form and runs 1800 ms drawing, readiness-dependent hold, then 650 ms contour erasure. GUI stalls pause progression rather than skipping stages.
- Login remains the current full page until the logo completion signal. Login handoff uses no page snapshot, slide or crossfade; ordinary non-login transitions remain unchanged.
- Vault creation/unlock use the existing startup worker with bound arguments so Qt remains responsive.
- 63 related tests passed: logo/login/main-window, data-reset lifecycle and diagnostic startup integration.
- Mypy passed for all four affected source modules. Dedicated logo/login/window lint checks passed; app.py has an unrelated existing unused install_click_ripples import, left unchanged.
- Actual Qt frames inspected for draw, complete, erase and empty completion. Preview saved outside the repository in this chat's visualization folder.

## Final visual revision

- Remove the idle logo completely; also keep it hidden when retrying or confirming a password.
- Center the loading mark horizontally/vertically, including on window resize.
- Submission immediately paints a pure-black frame and holds it for 100 ms before drawing. No glow, baseline or login form remains in the loading scene.
- Keep the full draw/hold/erase completion gate; use a light outline independent of theme for contrast against black.

## Black veil easing revision

- Fade the black veil in over the unchanged login layout (240 ms), then play the full centered draw/hold/erase sequence.
- Once all paths disappear, switch to the workspace under an opaque veil and fade it out (320 ms); never slide either page.
- Retry/confirmation prompts use the same fade-out and stay disabled until it finishes.
- Use bounded frame clocks for both fades, handle resize/hide, and keep transition_active until the workspace veil is gone.

### Veil verification

- 56 logo/login/main-window/veil tests passed; ruff and mypy passed for the affected modules.
- Inspected six actual Qt frames: idle login, partially dimmed login, full logo on black, opaque handoff, partially revealed workspace, clear workspace. No static login logo or sliding page motion.

## Invalid-password feedback revision

- InvalidPassword immediately cancels the current logo sequence and dismisses/resets the black veil without playing a return fade or emitting completion signals.
- Restore the input and its focus immediately, then run the existing shake/inner-border error animation. No static logo is restored.
- Confirmation prompts retain their existing handling; successful login retains the complete draw/erase and veil fade-in/fade-out sequence.
- Tested errors during veil fade-in, path drawing and the readiness hold; hiding/showing does not revive cancelled timers and the next submission restarts cleanly.
- Final verification: 58 related tests passed, ruff passed, mypy passed for all three changed source modules.
