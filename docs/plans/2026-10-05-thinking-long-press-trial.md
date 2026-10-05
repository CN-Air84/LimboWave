# Thinking long-press trial implementation plan

Goal: Hold an unavailable level for 3000 ms to temporarily enable it for the current actual model, then offer explicit persistence after a successful foreground run.

- A disabled-row gesture cancels on release, moving away, popup close, capability refresh, or control disable. A progress strip indicates the hold.
- Runtime-only catalog overrides enable reasoning and the requested level; no configuration write occurs before confirmation. Temporary access ends on model/site change, settings reload, another level selection, or app restart.
- A run-context trial token and post-commit thinking_trial_finished event prevent success prompts for a different model, failed/aborted runs, or background requests. A trial prompts at most once after success.
- User-confirmed levels retain provenance separately from probe results. Update only the captured endpoint/model/level, preserving unrelated capabilities. Closing the prompt means no persistence.
- Verify UI gestures, temporary vs durable model maps, run outcomes, and real Pi/mock-provider requests. Protocol errors remain errors; do not silently map an unsupported level to a lower one.

## Verification

- 200 focused UI/domain/service/real-Pi tests passed, including a full 3000 ms gesture, cancellation, target scoping, failure/abort guards, actual max-effort transport, and removing runtime-only overrides.
- Full unit suite: 1399 passed, 6 platform skips, 1 failure in test_compression_markers.py::test_repeated_compression_and_rollback_keep_historical_boundaries[memory]. The compression ordering implementation was not changed in this task; the isolated test also fails.
- Ruff and strict mypy checks passed for the changed implementation.
- Visually inspected .var/thinking-trial-hold.png and .var/thinking-trial-confirm.png; the progress strip and confirmation copy fit without clipping.
- Real-provider credentials, live configuration, and user database were not modified. Runtime integration uses the local mock provider.
