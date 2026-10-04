# Request-log responsiveness and settings recovery

**Goal:** Keep settings usable with large or unreadable request logs, including interrupted transitions.
**Architecture:** Query bounded metadata-only pages; read selected run details on a service-owned executor. Poll futures and render only the active detail tab in bounded UI batches. Reuse the log viewer and normalize interrupted/hidden page animations. Drain readers before storage shutdown.
**Tech Stack:** Python, PySide6, SQLite, pytest-qt.

## Implementation / verification
1. Add failing animation tests for selecting the source page during fade-out and hiding during either animation phase.
2. Add a metadata-only paged repository query (SQLite and memory), service paging, and tests proving snapshots are not decrypted for list display.
3. Replace synchronous viewer loading with cancellable future polling, pagination/retry controls, stale-result rejection, and incremental active-tab rendering. Preserve snapshot redaction and full stored data.
4. Reuse the viewer across settings visits, refresh the current conversation, and normalize hidden animations. Drain service readers before database shutdown.
5. Run focused service, SQLite, log-viewer, animation and settings tests; run lint/type checks and broader suites where practical. Use synthetic data only, never modify user logs.

No commits or unrelated worktree changes are part of this fix.


## Additional root cause from the supplied traceback
- Reproduced `libshiboken: Internal C++ object (QWidget) already deleted` by dynamically creating QTreeWidget children with the application checkbox style, reveal, ripple and smooth-scroll filters installed. This needs no log payload.
- Reveal handled Polish/Show synchronously and resolved/mutated a viewport during its construction. Defer preparation to a parent-owned timer, retain only weak widget references, and check validity before and after resolving a surface.
- Consume events targeting destroyed receivers in all three filters; queued work skips deleted widgets. Keep effects enabled rather than hiding the error.
- The fresh-process reproducer previously failed (including a native access violation); it now exits cleanly. Added regression coverage for creation, viewport replacement, pending deletion and teardown.

## Verification so far
- Metadata benchmark (synthetic 300 runs, 128 KiB request/response per run): legacy full load ~546 ms, first 100-row metadata page ~5 ms on this machine. These are local observations, not performance guarantees.
- Initial broad offscreen unit/UI run: 1857 passed, 2 skipped, 6 failures in URL/DNS and pixel/font assertions outside the modified request-log flow. Do not claim an all-green full suite.
- Final focused regression run after the lifecycle fix: 175 passed (request-log paging/service/stream/viewer, settings navigation, animations, reveal/ripple/smooth-scroll).
- Ruff passed for all touched modules/tests; mypy passed for the changed UI/service and app shutdown integration.
- Synthetic log viewer screenshot inspected with a registered CJK font; list, detail tabs and pagination are visible. No user data was read, deleted or reset.
