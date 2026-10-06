# LAN Password Gate Frontend Implementation Plan

Goal: Paired devices must verify the vault password before fetching chat data, including over HTTPS.
Architecture: Strict session flags drive a password phase; every attempt creates a fresh one-use RSA-OAEP/SHA256 + AES-GCM envelope. HTTP requires explicit risk confirmation; password state stays ephemeral.
Tech stack: React, TypeScript, Zod, node-forge, Vitest, Playwright, real WebServer fixture.

1. Extend wire and internal session schemas and password challenge/verify client methods.
2. Implement pure-JS envelopes using crypto.getRandomValues exclusively, including forge seed sources.
3. Gate controller initialization/read callbacks and render a password form with HTTP risk acknowledgement.
4. Test cross-library decryption, missing random source, authorization state, errors, form clearing and privacy.
5. Add PasswordGate fake verifier to the real fixture and verify every real E2E before chat; build to web/dist and serve that in the test fixture only.
6. Run npm install, typecheck, unit, build (--outDir dist), E2E and inspect mobile layout. Do not modify production Python.
