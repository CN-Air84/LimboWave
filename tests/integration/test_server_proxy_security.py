"""Security boundary for the public HTTPS reverse proxy deployment mode."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from limbowave.server.web_config import ServerWebConfig
from limbowave.web.app import create_app


@pytest.fixture
def server_config():
    return ServerWebConfig("https://chat.example.test").web_config()


async def test_public_origin_uses_secure_cookie_and_exact_host(server_config) -> None:
    app = create_app(SimpleNamespace(), server_config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=server_config.origin
    ) as client:
        response = await client.get("/api/v1/pair/status")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Host": "127.0.0.1:8765"}, 400),
        ({"X-Forwarded-Proto": "http"}, 400),
        ({"X-Forwarded-Host": "evil.example"}, 400),
        ({"Forwarded": "proto=https;host=chat.example.test"}, 400),
    ],
)
async def test_forged_proxy_boundary_is_rejected(server_config, headers, status) -> None:
    app = create_app(SimpleNamespace(), server_config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=server_config.origin
    ) as client:
        response = await client.get("/healthz", headers=headers)
    assert response.status_code == status


async def test_cross_origin_write_is_rejected(server_config) -> None:
    app = create_app(SimpleNamespace(), server_config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=server_config.origin
    ) as client:
        response = await client.post(
            "/api/v1/pair",
            json={"ticket": "x", "name": "browser"},
            headers={"Origin": "https://evil.example"},
        )
    assert response.status_code == 403


def test_server_mode_requires_canonical_https_origin() -> None:
    for origin in ("http://chat.example.test", "https://chat.example.test/path", "https://u:p@x"):
        with pytest.raises(ValueError, match="canonical HTTPS"):
            ServerWebConfig(origin).web_config()
    with pytest.raises(ValueError, match=r"private IPv4|canonical HTTPS"):
        ServerWebConfig("https://chat.example.test", bind_host="0.0.0.0").web_config()

