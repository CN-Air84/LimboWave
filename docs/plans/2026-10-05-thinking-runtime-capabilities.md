# Thinking runtime capabilities implementation plan

Goal: Make selectable thinking levels agree with the active Pi model, without treating unprobed levels as verified.

1. Read local config and database read-only; never print credentials or edit user data.
2. Export verified OpenAI effort levels into Pi thinkingLevelMap; compare this map on catalog reload.
3. Expose Pi get_available_thinking_levels through AgentKernel. Refresh on startup, model switch and settings changes; intersect with configured levels in the toolbar.
4. Keep unknown runtime capabilities fail-closed; show truthful feedback rather than claiming a rolled-back selection succeeded.
5. Cover catalog derivation, runtime RPC and toolbar intersection with regression tests and real isolated Pi tests.

## Findings and verification

- The live SQLite database is reachable read-only; capability authority is config.json, not conversation tables. No user database/config edits or credential extraction were performed.
- Custom Gemini models with unknown reasoning support and no verified effort list default to reasoning=false in Pi, hence only off.
- A custom Gemini model with reasoning=true but no effort mapping exposes only the base five levels in Pi.
- A custom OpenAI-compatible model had verified low/high/max stored, but thinkingLevelMap was not exported. Pi silently clamped max to high.
- Pi exposes get_available_thinking_levels. The UI now intersects this with configured levels, fails closed before the runtime read, and refreshes on startup/model switch/settings reload.
- OpenAI verified effort maps are exported; map-only reloads reassert the active model. Budget-only Google/Anthropic probes are not reinterpreted as discrete effort support.
- 84 focused unit/UI/real-Pi integration tests passed, including proof that max remains selected and the local mock provider receives reasoning_effort=max. No live paid-provider requests were made.
