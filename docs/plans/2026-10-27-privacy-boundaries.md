# PRIV-02 Implementation Plan

**Goal:** Add a non-destructive fresh conversation and persistent, application-enforced topic exclusions.

**Architecture:** Store policies in encrypted memory documents (included in existing backups). Rebuild the model-visible historical prefix from authoritative allowed messages on every send/retry; never trust restored runtime mirrors or old compactions. Apply the same policy to memory retrieval, chat search, compression and title generation. Rule labels never become model instructions.

**Tech Stack:** Python application services, SQLite encrypted documents, PySide6, Pi TypeScript extension, pytest.

## Boundary decisions
- A fresh conversation retains product/system rules but excludes global personal memories and foreign chat history. New conversation-local memory remains available.
- Topic rules use explicit labels/aliases and optional message/memory IDs, normalized with Unicode NFKC and case folding. No claim of exhaustive semantic/synonym recognition or physical erasure.
- Older assistant outputs and model-created memories with unknown provenance, all scratchpads and compression overlays are quarantined when exclusions exist. Original records remain visible to the user.
- No edits while a run/transition is active. Already transmitted content cannot be recalled. Existing backups may predate rules; restoration must warn and require acknowledgement.

## Steps
1. Add validated privacy policy and encrypted repository-backed service; test persistence, scopes, normalization and malformed-policy fail-closed behavior.
2. Filter memory/chat retrieval and isolated compression/title inputs; test forbidden IDs, fresh conversations and old derived content.
3. Add coordinator fresh-start creation and per-attempt privacy context. Rebuild historical prefix in the final Pi context hook, after memory/attachment injection; test retry/restore and tool-result suppression.
4. Add a privacy tab with topic create/remove controls and a fresh-chat action. Block rule changes during runs, show limitations and current mode.
5. Document backup/restore and capability limits. Run focused unit/integration/UI tests, lint and type checks; fix regressions.

## Verification
Use `.venv/Scripts/python.exe -m pytest` for new privacy tests and existing memory, chat-history, compression, runtime and UI suites. Use `.venv/Scripts/ruff.exe check` and `.venv/Scripts/mypy.exe` on modified Python modules; exercise the actual TypeScript extension through existing Pi integration fixtures if runtime is available.

## Implementation/verification notes
- Delivered encrypted global rules, durable fresh conversations, source-copy fingerprints, gateway output filtering and the final Pi projection/outbound guard.
- Native Pi compaction is cancelled under protection; missing projection explicitly aborts provider requests because hook exceptions alone are not fail-closed.
- UI inspected at 620px with the actual theme/CJK font. Hidden privacy forms clear topic text and ignore stale background reads.
- Focused UI/memory/tool/title/compression/retry regression run: 278 passed. Additional real Pi and storage suites passed; final focused rerun includes native-compaction cancellation and automatic retry.
- Ruff and strict mypy pass for modified feature modules. The broader architecture suite exposes an existing Qt import in application/services/preferences_service.py (also present in HEAD); it is outside this feature and left untouched.
- Unrelated reminder/settings/security work appeared concurrently in the shared checkout and was preserved; no bulk reset or commit was performed.


## P1 follow-up handoff (2026-10-27)
- Owned scope: `domain/privacy.py`, only privacy pre-invoke/output blocks and imports in `application/services/tool_gateway.py`, independent `tests/unit/test_privacy_tool_boundaries.py`, existing privacy Pi tests and this documentation. `privacy_service.py`, HIST files, CAP projection/CUSTOM logic, NET backend/transport, OOBE and commits were not changed in this follow-up.
- Red reproductions: 4 network-parameter cases executed forbidden queries; 3 nested-source cases passed; 3 encoded URL cases bypassed; redirect stub executed a prohibited hop. All now pass.
- NET interface integrated after its owner landed it: `guard_network_request(check_url: Callable[[str], None])`; synchronous scope wraps actual invocation, installed inside execution context; NET owns per-hop pre-DNS calls and constant-safe error conversion.
- Final bounded verification: **260 passed in 27.01s**, no skips/errors. Includes dual-backend privacy unit cases, network guard/transport/CAP/tool tests and **5 real Pi tests** (privacy and CAP) against the local mock provider.
- Ruff checks passed for both edited production modules and both privacy test files; strict mypy passed for the two edited production modules.
- Both assigned P1 issues READY for coordinated merge acceptance. HIST P1 remains owned by HIST and is not claimed complete here. No exhaustive semantic privacy promise; no real internet/paid API/actual credentials used.
