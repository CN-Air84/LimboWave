"""One-use password envelopes. This is NOT TLS or protection from active HTTP MITM.

Only paired devices can obtain a challenge. RSA-OAEP wraps a random AES-GCM key;
no reusable password verifier, vault salt or database key is exposed to browsers.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from collections.abc import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from limbowave.application.background import run_blocking

from .auth import AuthError, AuthStore, Device


class PasswordError(AuthError):
    """A generic failure; never leak whether decrypt, expiry or password failed."""


class PasswordGate:
    def __init__(self, verifier: Callable[[str], bool]) -> None:
        import asyncio

        self.verifier = verifier
        self._lock = asyncio.Lock()
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = (
            self._private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        self._challenges: dict[str, tuple[str, float]] = {}
        self._attempts: dict[str, int] = {}
        self._window, self._count = time.monotonic(), 0

    def clear(self) -> None:
        self._challenges.clear()
        self._attempts.clear()

    def forget(self, device_id: str) -> None:
        self._challenges.pop(device_id, None)
        self._attempts.pop(device_id, None)

    def challenge(self, device: Device) -> dict[str, object]:
        identity = secrets.token_urlsafe(32)
        self._challenges[device.id] = identity, time.monotonic() + 120
        return {"challenge_id": identity, "public_key": self.public_key, "expires_in": 120}

    async def verify(
        self,
        auth: AuthStore,
        device: Device,
        *,
        challenge_id: str,
        encrypted_key: str,
        iv: str,
        ciphertext: str,
    ) -> bool:
        # Never queue memory-expensive Argon2 work; reject concurrent attempts.
        if self._lock.locked():
            raise AuthError()
        async with self._lock:
            now = time.monotonic()
            if now - self._window >= 60:
                self._window, self._count = now, 0
            self._count += 1
            attempts = self._attempts.get(device.id, 0) + 1
            self._attempts[device.id] = attempts
            saved = self._challenges.pop(device.id, None)
            if self._count > 30 or attempts > 5:
                auth.revoke(device.id)
                raise AuthError()
            try:
                if (
                    saved is None
                    or now >= saved[1]
                    or not hmac.compare_digest(saved[0], challenge_id)
                    or not auth.valid(device)
                ):
                    raise ValueError()
                wrapped = base64.b64decode(encrypted_key, validate=True)
                nonce = base64.b64decode(iv, validate=True)
                sealed = base64.b64decode(ciphertext, validate=True)
                if len(wrapped) != 256 or len(nonce) != 12 or not 17 <= len(sealed) <= 4112:
                    raise ValueError()
                aes_key = self._private.decrypt(
                    wrapped,
                    padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                password = AESGCM(aes_key).decrypt(nonce, sealed, challenge_id.encode()).decode()
                accepted = bool(password) and await run_blocking(self.verifier, password)
                password = ""  # Python cannot promise secure memory erasure.
            except Exception:
                accepted = False
            if not auth.valid(device):
                return False
            if accepted:
                device.password_verified = True
                self._attempts.pop(device.id, None)
            elif attempts >= 5:
                auth.revoke(device.id)
            return accepted
