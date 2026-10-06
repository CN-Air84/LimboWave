"""Password gate must precede every sensitive API, including SSE and commands."""

import base64
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from limbowave.infrastructure.crypto.lan_password_verifier import VaultPasswordVerifier
from limbowave.infrastructure.crypto.vault import KdfParams, Vault
from limbowave.web.app import create_app
from limbowave.web.auth import AuthStore
from limbowave.web.password_gate import PasswordGate
from limbowave.web.security import WebConfig
from limbowave.web.server import WebServer


def envelope(challenge, password="vault-password"):
    import secrets

    key, iv = secrets.token_bytes(32), secrets.token_bytes(12)
    public = serialization.load_pem_public_key(challenge["public_key"].encode())
    encoded = public.encrypt(
        key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    sealed = AESGCM(key).encrypt(iv, password.encode(), challenge["challenge_id"].encode())
    return {
        "challenge_id": challenge["challenge_id"],
        "encrypted_key": base64.b64encode(encoded).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(sealed).decode(),
    }


async def test_paired_cookie_cannot_read_or_write_before_password_and_replay_fails():
    auth = AuthStore()
    gate = PasswordGate(lambda p: p == "vault-password")

    async def state():
        return {"available": True, "server_epoch": "test", "seq": 0, "stream": None}

    config = WebConfig()
    app = create_app(SimpleNamespace(epoch="test", state=state), config, auth, gate)
    issued = auth.issue("phone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        headers = {"Origin": config.origin, "X-CSRF-Token": issued["csrf_token"]}
        session = (await c.get("/api/v1/session")).json()
        assert session["password_required"] and not session["permissions"]["chat"]
        for path in [
            "/state",
            "/models",
            "/conversations",
            "/conversations/c/messages",
            "/events",
            "/commands/old",
        ]:
            assert (await c.get("/api/v1" + path)).status_code == 403
        assert (await c.post("/api/v1/commands", json={}, headers=headers)).status_code == 403
        challenge = (await c.get("/api/v1/password/challenge")).json()
        payload = envelope(challenge)
        assert "vault-password" not in str(payload)
        assert (await c.post("/api/v1/password/verify", json=payload)).status_code == 403
        assert (
            await c.post("/api/v1/password/verify", json=payload, headers=headers)
        ).status_code == 200
        assert not (await c.get("/api/v1/session")).json()["password_required"]
        assert (await c.get("/api/v1/state")).status_code == 200
        assert (
            await c.post("/api/v1/password/verify", json=payload, headers=headers)
        ).status_code == 401
        auth.revoke(issued["device_id"])
        assert (await c.get("/api/v1/state")).status_code == 401
        assert gate._challenges == gate._attempts == {}


async def test_wrong_password_invalid_envelope_and_attempt_limit():
    auth = AuthStore()
    gate = PasswordGate(lambda p: p == "correct")
    issued = auth.issue("phone")
    device = auth.authenticate(issued["token"])
    for i in range(5):
        challenge = gate.challenge(device)
        payload = envelope(challenge, "incorrect")
        if i == 1:
            payload["ciphertext"] = "not base64"
        assert not await gate.verify(auth, device, **payload)
        assert not device.password_verified
    assert device.revoked.is_set()


async def test_expired_challenge_and_cross_device_proof_denied(monkeypatch):
    auth, gate = AuthStore(), PasswordGate(lambda p: True)
    one = auth.authenticate(auth.issue("one")["token"])
    two = auth.authenticate(auth.issue("two")["token"])
    challenge = gate.challenge(one)
    assert not await gate.verify(auth, two, **envelope(challenge))
    gate._challenges[one.id] = (challenge["challenge_id"], 0)
    assert not await gate.verify(auth, one, **envelope(challenge))


def test_verifier_checks_current_vault_password_without_changing_unlocked_key(tmp_path):
    path = tmp_path / "vault.json"
    vault = Vault(path, params=KdfParams(time_cost=1, memory_cost=1024, parallelism=1))
    original = vault.create("old-password")
    verifier = VaultPasswordVerifier(path, original)
    assert verifier("old-password")
    assert not verifier("incorrect")
    vault.change_password("old-password", "new-password")
    assert not verifier("old-password")
    assert verifier("new-password")
    assert vault.require_key().key_bytes() == original.key_bytes()


async def test_http_opt_in_does_not_disable_other_checks_or_password_gate():
    WebConfig(host="192.168.1.2", allow_insecure_http=True).validate()
    with pytest.raises(ValueError):
        WebConfig(host="192.168.1.2").validate()
    with pytest.raises(ValueError):
        WebConfig(host="0.0.0.0", allow_insecure_http=True).validate()
    with pytest.raises(ValueError):
        WebConfig(host="192.168.1.2", allow_insecure_http=True, certfile="only.pem").validate()
    web = WebServer(
        SimpleNamespace(epoch="test"), WebConfig(host="192.168.1.2", allow_insecure_http=True)
    )
    with pytest.raises(ValueError, match="密码"):
        await web.start()
    assert not web.running and web._socket is None


async def test_revocation_while_password_verifier_runs_never_grants_access():
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()

    def slow(password):
        started.set()
        release.wait(3)
        return True

    auth, gate = AuthStore(), PasswordGate(slow)
    device = auth.authenticate(auth.issue("phone")["token"])
    payload = envelope(gate.challenge(device))
    task = asyncio.create_task(gate.verify(auth, device, **payload))
    try:
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        another = envelope(gate.challenge(device))
        from limbowave.web.auth import AuthError

        with pytest.raises(AuthError):
            await gate.verify(auth, device, **another)
        auth.revoke(device.id)
        release.set()
        assert not await task
        assert not device.password_verified
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_lan_http_transport_flags_and_no_password_bypass():
    auth, gate = AuthStore(), PasswordGate(lambda p: p == "vault-password")
    config = WebConfig(host="192.168.1.2", allow_insecure_http=True)

    async def state():
        return {"available": True, "seq": 0, "server_epoch": "epoch"}

    app = create_app(SimpleNamespace(epoch="epoch", state=state), config, auth, gate)
    issued = auth.issue("phone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        result = await c.get("/api/v1/session")
        assert result.json()["transport_secure"] is False
        assert result.json()["password_required"] is True
        assert (await c.get("/api/v1/state")).status_code == 403
        assert (
            await c.post(
                "/api/v1/password/verify",
                json={"password": "vault-password"},
                headers={"Origin": config.origin, "X-CSRF-Token": issued["csrf_token"]},
            )
        ).status_code == 422
        assert (await c.get("/api/v1/state", headers={"Host": "evil.test"})).status_code == 400
        challenge = (await c.get("/api/v1/password/challenge")).json()
        payload = envelope(challenge)
        payload["ciphertext"] = base64.b64encode(b"X" * 30).decode()
        error = await c.post(
            "/api/v1/password/verify",
            json=payload,
            headers={"Origin": config.origin, "X-CSRF-Token": issued["csrf_token"]},
        )
        assert error.status_code == 401 and error.json()["code"] == "vault_password_failed"
        assert "vault-password" not in error.text
        assert (await c.get("/api/v1/state")).status_code == 403
        challenge = (await c.get("/api/v1/password/challenge")).json()
        success = await c.post(
            "/api/v1/password/verify",
            json=envelope(challenge),
            headers={"Origin": config.origin, "X-CSRF-Token": issued["csrf_token"]},
        )
        assert success.status_code == 200
        assert (await c.get("/api/v1/state")).status_code == 200
        gate.clear()
        auth.invalidate()
        assert (await c.get("/api/v1/state")).status_code == 401
