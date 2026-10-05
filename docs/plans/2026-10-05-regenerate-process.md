# Non-blocking regeneration

Goal: Move regeneration database scans, decryption, attachments, snapshots and commits off the Qt event loop.

Architecture: one warm spawn-based process with independent SQLite connections. The GUI keeps asynchronous model RPC and Qt widgets. Never pickle live coordinators, connections or widgets. Transfer key material only through multiprocessing IPC, not files or command-line arguments. In-memory test/smoke controllers retain the existing local implementation.

Plan:
1. Extract Qt-free history projection and storage preparation methods.
2. Add a whitelisted process worker; offload regeneration preparation, branch/run commits, memory preflight and finalization.
3. Reserve transitions across all awaits; reuse existing prefix widgets using prepared history in BRANCHED before USER.
4. Warm/close the child with the application; support frozen Windows entry points.
5. Test real process PID, encrypted persistence, busy guards, failure/cancellation/shutdown, event order and Qt timer responsiveness during blocked database writes. Run regression tests, Ruff and mypy.

Trade-off: retain the existing async kernel instead of copying live sockets into a second process. This isolates expensive storage/CPU work without duplicating authoritative live-run state. Startup cost is paid once, not at every click.

## Implemented and verified

- Warm process handles regeneration history/entry lookup, orphan-link repair, attachment recovery, branch/run transactions, memory and permission preflight, plus finalization snapshots. The live async model kernel remains in the GUI coordinator; it already communicates with the separate Pi process.
- USER carries known attachment IDs; the view no longer scans every request intent for a new bubble. Historical fallback uses the owning message/run for a targeted lookup.
- BRANCHED carries the prepared Qt-free prefix. Existing widgets are reused; if the paging window changed, incremental rendering completes before USER/model send, under the transition guard.
- New tests verify independent/reused PID, forbidden caller-process storage execution, SQLite-lock heartbeat/Qt timer responsiveness, duplicate prevention, process failure, cancellation/close, real encrypted app wiring, and delayed-prefix event ordering.
- Regression results: 297 application/unit tests and 40 UI tests passed with clean process exits. The chat-view module additionally reports 73 passing assertions but retains the pre-existing Windows Qt shutdown crash (0xC0000005, reproduced against HEAD in the earlier Fork task).
- Ruff and diff whitespace checks pass. Mypy reports only the same four baseline errors in unrelated existing lines; no new type errors from the process boundary.
