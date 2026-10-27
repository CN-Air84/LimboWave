# Approved Voice + Network OOBE Integration Plan

**Goal:** Integrate the approved voice and network introductions into real onboarding; do not introduce HIST-02 or unrelated feature tours.

**Architecture:** Promote offline educational tryouts into a production UI module independent of tools/demos. Keep the existing OOBE shell/navigation/animations and real model setup gate. Add lazy optional configuration pages backed by the already encrypted voice/network settings services; merely reading or skipping introductions does not enable features, write settings, record or network.

**Tech Stack:** Python, PySide6, existing Settings/Credential/Voice/Network services, pytest-qt.

## Tasks
1. Add failing tests for intro → welcome → provider/key/models → success → voice → network → chat, reverse/Escape navigation, no HIST page, lifecycle cleanup, optional configuration and unchanged model gates.
2. Extract reusable offline voice/network education widgets with type-safe, theme-token styling; preserve independent demo behavior without production imports from tools.
3. Add an ordered introduction sequence and two pages in OOBE copy/shell. Keep primary continue visible at minimum size and large fonts; stop simulated activity whenever navigating/hiding.
4. Wire real optional voice/network setup lazily in onboarding gate, with encrypted credentials, explicit save, inline failure, write-only passwords, no auto-test request, and return/skip paths.
5. Run OOBE/model setup/lifecycle plus voice/network/settings regression, strict checks, dark/light/minimum-size screenshot QA. Document what is shipped and outstanding hardware/provider validation.

## Boundaries
- User approved only the two reviewed introductions in this turn. HIST-02 remains excluded; other introductions are future work.
- Real tryout is not enabled during onboarding: educational interactions stay visibly simulated, and simulation grants never become app permissions.
- Configuration changes require explicit save; defaults off and skipping remains non-mutating.
- Preserve all shared-worktree changes. No Git commit, release build or deployment without further instruction.

## User correction
The two feature introductions belong **after adding and explicitly choosing a model**, not before provider setup. Success continues to voice, voice to network, and the network primary action finishes onboarding. Back/Escape preserve configured models.

## Concurrent search work
User is opening a separate Bing implementation chat. This task owns only OOBE source/test/documentation. Do not modify network backend, provider enums, credential services, permission rules or production network settings UI. The live onboarding uses the existing NetworkSettingsPage factory so subsequent search-provider changes can flow through without duplicating provider configuration. Specific Bing copy in the educational module must be revisited after the search task lands; no claims of official direct Bing support are introduced here.

## Implementation status
- Approved introductions now live after explicit model selection; network is the final start-chat action. No HIST-02 introduction added.
- Native educational widgets moved to ui/oobe_companion.py; production imports no tools/demos modules. Original independent demo remains a non-persistent review surface.
- Lazy optional editors are wired to existing encrypted services; explicit save/no request, secret clearing and save-error skip paths tested.
- Reverse/Escape navigation, active-state cleanup, real model completion gate, 900×700 + 1.5× fonts and dark/light screenshots verified.
- Final combined same-snapshot suite: **166 passed** (96.32 seconds), including OOBE/demo, model completion, optional save/skip/secret clearing, restart lifecycle, voice configuration/controller and onboarding service tests. Ruff and strict targeted mypy passed for all four OOBE source modules. Log: `.tmp/oobe-final-approved-suite.log`.
- Search implementation files are reserved for the Azure Bing/Google chats. This task changed only OOBE and its tests/documentation; no commit/build/deployment.
