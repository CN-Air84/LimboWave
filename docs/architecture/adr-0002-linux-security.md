# ADR-0002: Linux security capability policy

Status: accepted for initial Linux support (2026-10-04).

Windows keeps DPAPI and Windows Hello behavior. Linux supports password-derived vault encryption and password-encrypted backups, but does not expose Windows recovery or in-app destructive reset. Reset continues to require the service's existing verified Windows Hello gate; UI must never bypass it.

Rejected for this iteration: silently password-only reset (weakens existing policy); desktop keyring presented as fresh identity verification (Secret Service does not promise it); ad-hoc PAM/polkit dialogs (distribution-specific policy, packaging and authentication requirements need independent design and validation).

Consequences: no new secret-storage dependency or plaintext key fallback. Migrated DPAPI wraps remain unusable on Linux, but ordinary password wraps remain usable; removing obsolete recovery wraps remains possible after password authentication. Linux reset is explicitly unavailable, not claimed complete. A future identity backend must prove fresh user consent, cancellation/failure behavior and packaging before enabling reset.
