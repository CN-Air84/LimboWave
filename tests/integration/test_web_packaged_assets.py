"""Mock frozen layout + real ASGI router; no npm, sockets or freezer invocation."""
from __future__ import annotations

import sys
from unittest.mock import Mock

import httpx
import pytest

from limbowave.web import app as web_app
from limbowave.web.security import WebConfig


@pytest.fixture
async def packaged_client(tmp_path, monkeypatch):
    bundle = tmp_path / "_MEI-test"
    module = bundle / "limbowave" / "web" / "app.py"
    static = module.parent / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><script src="/assets/app-test.js"></script>', encoding="utf-8",
    )
    (static / "assets" / "app-test.js").write_text("export {};", encoding="utf-8")
    (static / "assets" / "app-test.css").write_text("body{color:red}", encoding="utf-8")
    (module.parent / "private.txt").write_text("NOT PUBLIC", encoding="utf-8")
    monkeypatch.setattr(web_app, "__file__", str(module))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.chdir(tmp_path)  # Never resolve relative to the caller's cwd.
    facade = Mock()
    app = web_app.create_app(facade, WebConfig())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
    ) as client:
        yield client, static, facade


async def test_frozen_index_and_asset_mime(packaged_client):
    client, _, facade = packaged_client
    for path, mime, body in [
        ("/", "text/html", "<!doctype html>"),
        ("/assets/app-test.js", "javascript", "export {};"),
        ("/assets/app-test.css", "text/css", "body{color:red}"),
    ]:
        response = await client.get(path)
        assert response.status_code == 200
        assert mime in response.headers["content-type"]
        assert body in response.text
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
    assert not facade.mock_calls


@pytest.mark.parametrize("path", [
    "/api", "/api/v1/not-a-route", "/assets/missing.js", "/missing-page",
    "/docs", "/redoc", "/openapi.json", "/%2e%2e%2fprivate.txt",
])
async def test_missing_routes_never_fall_back_to_index(packaged_client, path):
    client, _, _ = packaged_client
    response = await client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "<!doctype html>" not in response.text
    assert "NOT PUBLIC" not in response.text


async def test_api_precedes_static_and_keeps_auth(packaged_client):
    client, static, facade = packaged_client
    shadow = static / "api" / "v1" / "state"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("SHADOW", encoding="utf-8")
    response = await client.get("/api/v1/state")
    assert response.status_code == 401
    assert "SHADOW" not in response.text
    assert not facade.mock_calls
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"ok": True}


async def test_missing_packaged_entry_does_not_redirect_to_dev_server(packaged_client):
    client, static, _ = packaged_client
    (static / "index.html").unlink()
    response = await client.get("/")
    assert response.status_code == 404
    assert "location" not in response.headers
