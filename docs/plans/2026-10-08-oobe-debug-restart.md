# OOBE Debug Restart Implementation Plan

**Goal:** Ctrl+Shift+O restarts LimboWave and forces the real OOBE for that launch, without resetting user data.

**Architecture:** MainWindow emits a dedicated restart intent. The application exits through its existing shutdown/cleanup path, then starts the same executable (or Python module) with a one-shot `--oobe` argument. Startup consumes that argument and bypasses only the configured-model check, not vault unlocking. No persistent debug setting, shell command, credential argument, or data deletion is introduced.

**Tech Stack:** Python, PySide6 QAction, qasync, subprocess, pytest/pytest-qt.

## Approach

Prefer a real process restart over swapping in the OOBE page (which would retain an active kernel) or clearing config (which would destroy user state). Preserve original launch arguments and support both source and packaged Windows launches. Repeated shortcut presses must not spawn multiple children; a refused window close must cancel the restart.

## Tasks

1. Add failing tests for the shortcut, source/frozen relaunch commands, force-OOBE startup, and cleanup-before-relaunch ordering using temporary data and fake process launchers.
2. Add `oobe_restart_requested` / Ctrl+Shift+O in `src/limbowave/ui/main_window.py`; implement `--oobe`, graceful restart intent, and post-cleanup relaunch in `src/limbowave/app.py`. Keep ordinary launch/smoke/reset behavior unchanged.
3. Test the real Qt startup lifecycle in isolated child processes, with mocked vault/model/network operations. Cover configured startup, OOBE reentry, skipped/cancelled OOBE, and shutdown failure. Do not launch a real user instance or touch its data.
4. Document the shortcut in `docs/development/oobe.md`; run focused OOBE/startup/diagnostics tests and lint/type checks. Leave unrelated local edits intact and do not commit as part of this request.

## Completion

Implemented the shortcut, one-launch `--oobe` flag, data-preserving shutdown/relaunch, source/frozen process helper, failure handling, and development documentation. Added 16 passing tests; 69 related tests passed in grouped runs. Lint and targeted type checks passed. Two unrelated expanded-suite failures reproduce on HEAD `22eebc5`; two other combined-suite failures pass in isolation. No user data was accessed, no live user process was restarted, and no commit/package was produced.

## Follow-up: development-only gate

Only a non-frozen process loading LimboWave from its `src/limbowave` Git checkout (including worktrees) may use the debug entry. Ordinary installed packages and frozen releases fail closed. Detect this in bootstrap, register the shortcut only when enabled, reject restart intents and forced `--oobe` outside development, and guard the process launcher as well. Preserve automatic first-use onboarding in every environment. Add environment matrix and release lifecycle regression tests.

Development-only follow-up completed: bootstrap now detects an unfrozen source Git checkout, MainWindow defaults to no debug shortcut, release GUI paths reject the flag and signal, and the launcher fails closed. The ordinary first-use route remains unchanged. Environment/shortcut/launcher/lifecycle tests: 35 passed; lint and targeted type checks passed.
