from types import SimpleNamespace

import httpx
import pytest

from limbowave.web.app import create_app
from limbowave.web.auth import AuthStore
from limbowave.web.security import WebConfig


@pytest.fixture
def backend():
    config = WebConfig()
    auth = AuthStore()
    facade = SimpleNamespace(epoch="test-epoch")
    return create_app(facade, config, auth), auth, config


async def pair_client(client, auth):
    bootstrap = await client.get("/api/v1/pair/status")
    ticket = auth.open_pairing()["ticket"]
    response = await client.post(
        "/api/v1/pair",
        json={"ticket": ticket},
        headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": bootstrap.json()["csrf_token"]},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "8.8.8.8", "169.254.1.2", "::1", "localhost", "240.1.2.3"]
)
def test_unsafe_bind_rejected(host):
    with pytest.raises(ValueError):
        WebConfig(host=host).validate()


def test_lan_requires_tls():
    with pytest.raises(ValueError, match="TLS"):
        WebConfig(host="192.168.1.2").validate()
    WebConfig().validate()


async def test_auth_host_origin_csrf_and_secret_free_errors(backend):
    app, auth, config = backend
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        assert (await c.get("/api/v1/state")).status_code == 401
        assert (await c.get("/healthz", headers={"Host": "evil.test"})).status_code == 400
        assert (await c.post("/api/v1/pair", json={})).status_code == 403
        session = await pair_client(c, auth)
        assert "token" not in session
        cookie = c.cookies.get("lw_session")
        response = await c.post("/api/v1/logout", headers={"Origin": config.origin})
        assert response.status_code == 403
        headers = {"Origin": config.origin, "X-CSRF-Token": session["csrf_token"]}
        assert (
            await c.post("/api/v1/logout", headers={**headers, "Origin": "null"})
        ).status_code == 403
        assert (await c.post("/api/v1/logout", headers=headers)).status_code == 200
        response = await c.get("/api/v1/session")
        assert response.status_code == 401
        assert cookie not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_body_limit_static_boundaries_and_no_docs(backend):
    app, _auth, config = backend
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        r = await c.post("/api/v1/pair", content=b"x" * 65537, headers={"Origin": config.origin})
        assert r.status_code == 413
        for path in ("/docs", "/openapi.json", "/api/v1/not-real", "/..%2fapp.py"):
            assert (await c.get(path)).status_code == 404
        assert (await c.get("/api/v1/events?token=secret")).status_code == 400


async def test_manual_code_confirmation_and_cookie_flags(backend):
    app, auth, config = backend
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        csrf = (await c.get("/api/v1/pair/status")).json()["csrf_token"]
        code = auth.open_pairing()["code"]
        response = await c.post(
            "/api/v1/pair",
            json={"code": code},
            headers={"Origin": config.origin, "X-CSRF-Token": csrf},
        )
        assert response.json()["status"] == "pending"
        assert "proof" not in response.json()
        assert "httponly" in response.headers["set-cookie"].lower()
        assert (await c.get("/api/v1/pair/status")).json()["status"] == "pending"
        auth.approve(response.json()["request_id"])
        assert (await c.get("/api/v1/pair/status")).json()["status"] == "paired"
        assert (await c.get("/api/v1/session")).status_code == 200


async def test_concurrency_rate_chunked_body_and_revocation_during_read():
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    async def state():
        entered.set()
        await release.wait()
        return {"available": True, "stream": {"text": "PRIVATE"}}

    auth = AuthStore()
    config = WebConfig(max_requests=1)
    app = create_app(SimpleNamespace(state=state), config, auth)
    issued = auth.issue("phone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        waiting = asyncio.create_task(c.get("/api/v1/state"))
        await entered.wait()
        assert (await c.get("/healthz")).status_code == 429
        auth.revoke(issued["device_id"])
        release.set()
        response = await waiting
        assert response.status_code == 401 and "PRIVATE" not in response.text
        assert response.headers["cache-control"] == "no-store"

    config = WebConfig(requests_per_minute=1)
    app = create_app(SimpleNamespace(), config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/healthz")).status_code == 429

    async def chunks():
        yield b"a" * 40000
        yield b"b" * 40000

    config = WebConfig()
    app = create_app(SimpleNamespace(), config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        response = await c.post("/api/v1/pair", content=chunks(), headers={"Origin": config.origin})
        assert response.status_code == 413


def make_certificate(tmp_path, ip="127.0.0.1", expired=False):
    import ipaddress
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_tls_certificate_ip_san_and_expiration(tmp_path):
    cert, key = make_certificate(tmp_path, ip="192.168.1.23")
    WebConfig(host="192.168.1.23", certfile=cert, keyfile=key).validate()
    with pytest.raises(ValueError, match="IP SAN"):
        WebConfig(host="192.168.1.24", certfile=cert, keyfile=key).validate()
    cert, key = make_certificate(tmp_path, expired=True)
    with pytest.raises(ValueError, match="validity"):
        WebConfig(certfile=cert, keyfile=key).validate()


async def test_real_https_sets_secure_cookie_and_logs_no_access(tmp_path, caplog):
    import ssl

    from limbowave.web.server import WebServer

    cert, key = make_certificate(tmp_path)
    facade = SimpleNamespace(epoch="test", rotate_epoch=lambda: None)
    web = WebServer(facade, WebConfig(port=0, certfile=cert, keyfile=key))
    try:
        await web.start()
        context = ssl.create_default_context(cafile=cert)
        async with httpx.AsyncClient(verify=context, base_url=web.url) as client:
            csrf = (await client.get("/api/v1/pair/status")).json()["csrf_token"]
            ticket = web.open_pairing()["ticket"]
            response = await client.post(
                "/api/v1/pair",
                json={"ticket": ticket},
                headers={"Origin": web.url, "X-CSRF-Token": csrf},
            )
            assert response.status_code == 200
            cookies = response.headers.get_list("set-cookie")
            session_cookie = next(c for c in cookies if c.startswith("lw_session="))
            assert "Secure" in session_cookie and "HttpOnly" in session_cookie
            assert "SameSite=strict" in session_cookie
            assert ticket not in caplog.text
            assert not any(record.name == "uvicorn.access" for record in caplog.records)
    finally:
        await web.stop()


@pytest.mark.parametrize("cursor", ["bad", "epoch:", ":1", "epoch:-1", "epoch:x", "epoch:1:2", ""])
async def test_invalid_sse_cursor_is_json_400_before_start(backend, cursor):
    app, auth, config = backend
    issued = auth.issue("phone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        result = await c.get("/api/v1/events", headers={"Last-Event-ID": cursor})
        assert result.status_code == 400
        assert result.headers["content-type"].startswith("application/json")
        assert app.state.streams == 0
