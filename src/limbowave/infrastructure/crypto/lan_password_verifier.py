"""Read-only verification against the current vault password wrapping.

The LAN layer receives only a callable returning bool, never the unlocked key.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from limbowave.infrastructure.crypto.vault import Vault, VaultKey


class VaultPasswordVerifier:
    """Never mutate the desktop vault or publish KDF/key material to the browser."""

    def __init__(self, path: Path, key: VaultKey) -> None:
        self.path, self.key = path, key

    def __call__(self, password: str) -> bool:
        try:
            candidate = Vault(self.path)
            try:
                return hmac.compare_digest(
                    candidate.unlock(password).key_bytes(), self.key.key_bytes()
                )
            finally:
                candidate.lock()
        except Exception:
            return False
