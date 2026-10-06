import pytest

from limbowave.web.auth import AuthError, AuthStore, digest


def test_ticket_once_digest_only_and_revocation():
    auth = AuthStore()
    pair = auth.open_pairing()
    issued = auth.pair(ticket=pair["ticket"])
    token = issued["token"]
    device = auth.authenticate(token)
    assert device.token_hash == digest(token)
    assert token not in repr(auth.devices)
    assert pair["ticket"] not in repr(vars(auth))
    with pytest.raises(AuthError):
        auth.pair(ticket=pair["ticket"])
    auth.revoke(device.id)
    assert device.revoked.is_set()
    with pytest.raises(AuthError):
        auth.authenticate(token)


def test_manual_pair_requires_desktop_approval_and_once():
    auth = AuthStore()
    pair = auth.open_pairing()
    pending = auth.pair(code=pair["code"])
    assert auth.status(pending["proof"])["status"] == "pending"
    assert auth.list_devices() == []
    auth.approve(pending["request_id"])
    assert auth.status(pending["proof"])["status"] == "paired"
    with pytest.raises(AuthError):
        auth.status(pending["proof"])


def test_expiry_attempt_budget_and_invalidation():
    now = [0.0]
    auth = AuthStore(clock=lambda: now[0], idle_ttl=5, absolute_ttl=10)
    pair = auth.open_pairing()
    issued = auth.pair(ticket=pair["ticket"])
    d = auth.authenticate(issued["token"])
    now[0] = 5
    assert not auth.valid(d)
    assert d.revoked.is_set()
    now[0] = 121
    with pytest.raises(AuthError):
        auth.pair(code=pair["code"])
    pair = auth.open_pairing()
    for _ in range(10):
        with pytest.raises(AuthError):
            auth.pair(code="000000000")
    with pytest.raises(AuthError):
        auth.pair(ticket=pair["ticket"])
    auth.invalidate()
    assert auth.list_pending() == []


def test_csrf_and_device_cap():
    auth = AuthStore(max_devices=1)
    issued = auth.issue("phone")
    d = auth.authenticate(issued["token"])
    auth.csrf(d, issued["csrf_token"])
    with pytest.raises(AuthError):
        auth.csrf(d, "bad")
    with pytest.raises(AuthError):
        auth.issue("overflow")
