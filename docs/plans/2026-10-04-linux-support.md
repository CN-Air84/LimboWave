# Linux support implementation plan

**Goal:** Add explicit Linux-compatible code paths without weakening Windows security.
**Architecture:** Native path semantics; Windows PowerShell and POSIX Bash/Sh backends; capability-gated system recovery/reset; platform-native packaging. Work in the existing dirty checkout to preserve current implementation, including untracked files. No automatic commits.
**Tech Stack:** Python 3.12+, PySide6, pytest, PyInstaller, Node/Pi.

## Decisions / alternatives
- Keep DPAPI/Hello unchanged on Windows. On Linux disable system recovery and destructive in-app reset with clear reasons; preserve password unlock and encrypted backups. Never downgrade a second-factor requirement to an unlocked desktop session.
- Secret Service stores secrets but does not guarantee fresh user verification. PAM/polkit integration varies across distributions and requires an explicit threat model; defer both rather than claim equivalent protection.
- Prefer Bash, fall back to POSIX sh on Linux. Do not require installing PowerShell or trust arbitrary SHELL values.

## Tasks (test first where possible)
1. Add platform semantics tests, then fix domain/permissions.py and domain/path_guard.py. Test case sensitivity, root containment, Windows names and Linux backslashes.
2. Add security capability tests; gate recovery settings, login recovery and reset UI. Keep service fail-closed. Document backups and unsupported recovery.
3. Extend domain/shell.py, shell/probe.py and executor.py; add POSIX process-group execution and timeout cleanup. Wire terminal gateway and pass real environment to Pi. Test output, failures, cwd, syntax diagnostics and child cleanup.
4. Extend pi_rpc.py CLI discovery for resolved symlinks and Node prefixes, with fake installation tests.
5. Adapt scripts/packager.py and tests for Linux output/data flags; add Linux bootstrap/run/verify scripts and desktop launch documentation.
6. Add Linux CI and platform-aware tests, run local ruff/mypy/pytest. Attempt Linux validation only if an existing Linux runtime is available. Record unverified desktop and packaging behavior explicitly.

## Acceptance / manual release gates
- Windows regressions pass; no credential persistence downgrade.
- Linux CI runs unit/integration/offscreen UI tests with Node and an installed Bash.
- Linux desktop release remains unverified until native X11/Wayland, Chinese IME, clipboard, scaling and packaged startup checks pass.

## Implementation / verification record

Implemented platform-native path authorization and reserved-name handling; explicit unavailable identity backend and UI/service fail-closed paths; Bash/Sh executor selection, process-group cleanup and gateway wiring; selected Node-prefix/symlink Pi discovery; native PyInstaller filenames/flags; Linux shell entry point and CI; migration and desktop guidance.

Local Windows verification:
- Final focused regression run: 205 passed, 6 skipped (native POSIX / symlink requirements), 17.14s.
- Tool bridge, reset lifecycle and startup responsiveness: 21 passed.
- Full checkout run: 1952 passed, 19 failed, 6 skipped, 4 teardown errors (469.73s). Failures include popup material/theme/hover/font and diagnostic-lock expectations. Not all causes have been attributed; this is NOT a green baseline or a Linux verification result.
- Targeted checks of edited Linux modules pass. Whole-tree mypy reports four errors in existing run_coordinator.py, chat_view.py, logical_models_tab.py. Whole-tree ruff also reports existing checkbox_style.py/theme.py/test_diagnostics_startup_cleanup.py issues. Do not suppress these in CI.
- No usable Linux distribution/container runtime is available on this machine. Native POSIX execution, Linux build and X11/Wayland acceptance remain pending. CI has been added but not pushed or executed.

Known deliberate scope: no Linux system recovery or destructive reset; no keyring/PAM substitute; command spooling has no hard disk quota and deliberately detached daemons are not contained. Node/Pi remain external prerequisites. No commits or pushes were made.
