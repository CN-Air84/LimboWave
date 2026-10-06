"""In-memory, digest-only pairing and revocable browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthError(Exception):
    pass


@dataclass
class Device:
    id: str
    name: str
    token_hash: str
    csrf_hash: str
    created: float
    touched: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    password_verified: bool = False


@dataclass
class Pending:
    id: str
    name: str
    proof_hash: str
    phrase: str
    expires: float
    approved: bool = False


class AuthStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_ttl: float = 7200,
        absolute_ttl: float = 43200,
        max_devices: int = 8,
        on_revoke: Callable[[str], None] | None = None,
    ) -> None:
        self.on_revoke = on_revoke
        self._csrf_key = secrets.token_bytes(32)
        self.clock = clock
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl
        self.max_devices = max_devices
        self.devices: dict[str, Device] = {}
        self.pending: dict[str, Pending] = {}
        self.ticket_hash = ""
        self.code_hash = ""
        self.window_expires = 0.0
        self.attempts = 0

    def open_pairing(self) -> dict[str, object]:
        ticket, code = secrets.token_urlsafe(32), f"{secrets.randbelow(100000000):08d}"
        self.ticket_hash, self.code_hash = digest(ticket), digest(code)
        self.window_expires = self.clock() + 120
        self.attempts = 0
        self.pending.clear()
        return {"ticket": ticket, "code": code, "expires_in": 120}

    def pair(
        self, *, ticket: str | None = None, code: str | None = None, name: str = "Browser"
    ) -> dict[str, object]:
        self.attempts += 1
        if self.clock() >= self.window_expires or self.attempts > 10:
            raise AuthError()
        if ticket and hmac.compare_digest(digest(ticket), self.ticket_hash):
            self.ticket_hash = ""
            return self.issue(name)
        if code and hmac.compare_digest(digest(code), self.code_hash):
            self.code_hash = ""
            proof = secrets.token_urlsafe(32)
            p = Pending(
                secrets.token_urlsafe(16),
                name,
                digest(proof),
                secrets.token_hex(3),
                self.window_expires,
            )
            self.pending[p.id] = p
            return {"status": "pending", "request_id": p.id, "proof": proof, "phrase": p.phrase}
        raise AuthError()

    def approve(self, request_id: str) -> None:
        p = self.pending.get(request_id)
        if p is None or self.clock() >= p.expires:
            raise AuthError()
        p.approved = True

    def status(self, proof: str) -> dict[str, object]:
        for p in list(self.pending.values()):
            if hmac.compare_digest(p.proof_hash, digest(proof)) and self.clock() < p.expires:
                if not p.approved:
                    return {"status": "pending", "phrase": p.phrase}
                del self.pending[p.id]
                return self.issue(p.name)
        raise AuthError()

    def issue(self, name: str) -> dict[str, object]:
        for d in list(self.devices.values()):
            self.valid(d)
        if len(self.devices) >= self.max_devices:
            raise AuthError()
        token = secrets.token_urlsafe(32)
        csrf = self.csrf_token(token)
        now = self.clock()
        d = Device(secrets.token_urlsafe(16), name[:80], digest(token), digest(csrf), now, now)
        self.devices[d.id] = d
        return {"status": "paired", "token": token, "csrf_token": csrf, "device_id": d.id}

    def valid(self, d: Device) -> bool:
        now = self.clock()
        if (
            d.revoked.is_set()
            or now - d.created >= self.absolute_ttl
            or now - d.touched >= self.idle_ttl
        ):
            self.revoke(d.id)
            return False
        return True

    def authenticate(self, token: str, *, touch: bool = True) -> Device:
        hashed = digest(token)
        for d in list(self.devices.values()):
            if hmac.compare_digest(d.token_hash, hashed) and self.valid(d):
                if touch:
                    d.touched = self.clock()
                return d
        raise AuthError()

    def csrf_token(self, token: str) -> str:
        return hmac.new(self._csrf_key, token.encode(), hashlib.sha256).hexdigest()

    def csrf(self, d: Device, value: str) -> None:
        if not hmac.compare_digest(d.csrf_hash, digest(value)):
            raise AuthError()

    def revoke(self, device_id: str) -> None:
        d = self.devices.pop(device_id, None)
        if d:
            d.revoked.set()
            if self.on_revoke:
                self.on_revoke(d.id)

    def invalidate(self) -> None:
        for device_id in list(self.devices):
            self.revoke(device_id)
        self.pending.clear()
        self.ticket_hash = self.code_hash = ""
        self.window_expires = 0

    def list_devices(self) -> list[dict[str, str]]:
        return [
            {"device_id": d.id, "name": d.name}
            for d in list(self.devices.values())
            if self.valid(d)
        ]

    def list_pending(self) -> list[dict[str, str]]:
        return [
            {"request_id": p.id, "name": p.name, "phrase": p.phrase}
            for p in self.pending.values()
            if self.clock() < p.expires
        ]
