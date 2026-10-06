# LimboWave mobile web (T5)

Root `web/` is the frontend source. Python owns authentication, transport policy and the runtime; this package does not reimplement the agent.

## Commands

Node **24+** (or 22.12+) and npm are required.

```sh
cd web
npm ci
npm test
npm run typecheck
npm run build -- --outDir dist
npm run test:e2e
```

The default production build writes to `../src/limbowave/web/static`. For frontend-only work and E2E, use `npm run build -- --outDir dist`; the fixture serves root `web/dist` without touching production files. No source maps or service worker. `emptyOutDir: false` intentionally avoids removing files owned by another task. Only reference current hashed assets from `index.html` when packaging. No Git commit is performed by these scripts.

The E2E runner starts **real WebServer + FastAPI + auth + RuntimeFacade + RunCoordinator + in-memory repositories**, substituting only the model kernel and vault verifier (`PasswordGate(lambda p: p == "test-vault-password")`), on loopback port 8877. The explicit `tests/backend.py` fixture is launched by a small Node process adapter; stdin EOF shuts down WebServer gracefully. No production Python files are modified. The repository `.venv` must include its normal development/web dependencies; set `LIMBOWAVE_PYTHON` to override the interpreter. Install Chromium with `npx playwright install chromium`. The test depends on the T1–T4 backend, including HistoryService pagination, being implemented. It does not silently replace missing backend methods with mocks.

`npm run dev` binds only loopback. Its API proxy is a convenience, not a security bypass: backend Host/Origin allowlists must explicitly match the development origin. For end-to-end security testing, use the built assets served directly by the backend. Never expose Vite to LAN as the production service.

## Contract and security

`src/api/adapter.ts` validates and translates the Python wire contract into internal typed DTOs. See `API-CONTRACT.md` for the exact boundary and pending choices. Components never import Python internals.

- Pair ticket is consumed from `#ticket=…` before React mounts, then the fragment is immediately removed. No ticket/token in query strings, localStorage, sessionStorage or console logs.
- Pairing cookies only preauthorize a device. `password_required` gates all chat reads and SSE until password verification succeeds and a fresh session confirms it. HTTPS also requires verification; missing security fields fail schema validation.
- Passwords are UTF-8 encrypted using forge RSA-OAEP SHA256 (including MGF1) wrapping a random 32-byte AES key, AES-GCM with 12-byte IV, UTF-8 challenge ID AAD and a trailing 16-byte tag. Standard Base64 is used. All randomness (including forge seed sources) comes from `crypto.getRandomValues`; there is no `subtle` or `Math.random` dependency in this path. No safe random source means fail closed. Every attempt requests a fresh challenge. Password input clears on submit and is never stored or logged.
- HTTP requires explicit risk acceptance: chats are unencrypted and modifiable; the encrypted password envelope does not prevent active MITM or malicious replacement of the page/public key. Trusted HTTPS is recommended.
- Authentication cookies are never read by JavaScript. `HttpOnly`, TLS-dependent `Secure`, `SameSite=Strict`, transport policy, Origin/Host validation, CSP, response no-store and revocation **must be enforced by Python**; a frontend cannot mint HttpOnly cookies or enforce transport security. Loopback HTTP E2E is not a LAN HTTPS acceptance test.
- CSRF bootstrap comes from `GET /api/v1/pair/status`; authenticated CSRF comes from `GET /api/v1/session`. POSTs use `X-CSRF-Token`, all fetches use same-origin credentials and no-store.
- SSE uses fetch with `Last-Event-ID: epoch:seq`; no auth parameters in its URL. Reads snapshot before subscription, handles gap/resync/epoch changes, and rebuilds from paged history plus unpersisted stream snapshot. Back/foreground and online events trigger reconciliation, not sends.
- Mutations are never automatically retried. On ambiguous POST failure, query a receipt; while pending, block new mutation attempts. A missing/unknown receipt is *not* evidence that an earlier POST failed. The UI conservatively keeps pending state until a definitive receipt, re-pair, or epoch change.
- Drafts are per conversation/branch, in page memory only. Conflict retains the draft. Switching history does not change backend runtime targets. Logout clears all local conversation content and drafts.
- Markdown uses `react-markdown`, no raw HTML plugins, no `dangerouslySetInnerHTML`. Image nodes become inert text; remote images are never fetched. Only explicit HTTP(S) links open with noopener/noreferrer. No remote fonts or assets.
- Streaming renders are coalesced to 40 ms. Scroll follows only near the bottom; older-history loading preserves its visual anchor. Thinking/tool activity is collapsed and never includes remote approval controls.
- Touch Enter inserts a newline; desktop Enter sends, Shift+Enter inserts a newline. Composition/isComposing/229 suppress send. Controls remain at least 44px and textarea font is 16px to avoid iOS autozoom.

## Acceptance still requiring a device

Trusted LAN HTTPS certificate installation, actual iOS/Android keyboard resizing, long background suspension, device revocation from desktop, and multi-device generation contention require the running desktop application and real phones. Automated loopback browser tests do not establish these claims.
## Verified locally

- Unit/component suite (expanded for password gate): tests (wire schemas, CSRF, fragment cleanup, sequence/epoch/terminal handling, resync, authoritative snapshot tool state, command ambiguity/409, IME, safe Markdown).
- Playwright: `mobile-chat.spec.ts` uses the actual built React app and real Python WebServer/kernel fixture for pairing/new/send/stream/history reload/stop/reconnect; `real-protocol.spec.ts` additionally checks explicit Last-Event-ID replay with no mocked routes.
- `mobile-ui.spec.ts` is separately labeled as a mocked UI contract fixture: 320/390/430/844 widths, IME, conflict drafts, pagination rendering and unsafe Markdown. It is not a replacement for the real chain.
- Typecheck, unit, root-web build and real E2E are run for this change. `npm audit` reports one high-severity node-forge advisory (PKCS#1 v1.5 signature verification, not used by the password envelope); no fix is currently offered by npm.

