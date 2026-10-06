"""Authenticated same-origin API and packaged static assets."""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException

from .auth import AuthError, AuthStore, Device
from .dto import page, project, safe_error
from .password_gate import PasswordError, PasswordGate
from .schemas import Command, PairRequest, PasswordEnvelope
from .security import SecurityMiddleware, WebConfig, error_response
from .stream import RevocableEventResponse, event_stream

COOKIE = "lw_session"
PENDING = "lw_pair"
BOOTSTRAP = "lw_bootstrap"


def create_app(
    facade: Any,
    config: WebConfig,
    auth: AuthStore | None = None,
    password_gate: PasswordGate | None = None,
) -> FastAPI:
    if not ipaddress.ip_address(config.host).is_loopback and password_gate is None:
        raise ValueError("局域网访问必须配置资料库密码核验")
    auth = auth or AuthStore()

    def on_revoke(device_id: str) -> None:
        if password_gate is not None:
            password_gate.forget(device_id)
        revoke = getattr(facade, "revoke_device", None)
        if revoke is not None:
            revoke(device_id)

    auth.on_revoke = on_revoke
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.accepting = True
    app.state.streams = 0
    app.add_middleware(SecurityMiddleware, config=config)

    def cookie(response: JSONResponse, name: str, value: str, age: int) -> None:
        response.set_cookie(
            name, value, max_age=age, httponly=True, secure=config.tls, samesite="strict", path="/"
        )

    def paired(result: dict[str, Any]) -> JSONResponse:
        result = dict(result)
        token = result.pop("token", None)
        proof = result.pop("proof", None)
        response = JSONResponse(result)
        if token:
            cookie(response, COOKIE, token, int(auth.absolute_ttl))
            response.delete_cookie(PENDING, path="/")
        if proof:
            cookie(response, PENDING, proof, 120)
        return response

    def device(request: Request, *, write: bool = False, unverified: bool = False) -> Device:
        if not app.state.accepting:
            raise HTTPException(503)
        try:
            d = auth.authenticate(request.cookies.get(COOKIE, ""))
            if write:
                auth.csrf(d, request.headers.get("x-csrf-token", ""))
            if password_gate is not None and not unverified and not d.password_verified:
                raise HTTPException(403)
            request.scope["web_device"] = d
            request.scope["web_auth"] = auth
            return d
        except AuthError:
            raise HTTPException(403 if write else 401) from None

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        codes = {
            400: "invalid_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            413: "body_limit",
            429: "request_limit",
            503: "unavailable",
        }
        return error_response(exc.status_code, codes.get(exc.status_code, "request_failed"))

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(422, "invalid_request")

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc: RuntimeError) -> JSONResponse:
        error = safe_error(
            {"code": getattr(exc, "code", None), "status": getattr(exc, "status", None)}
        )
        if error["code"] == "runtime_unavailable":
            auth.invalidate()
        return error_response(error["status"], error["code"])

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(
            401, "vault_password_failed" if isinstance(exc, PasswordError) else "pairing_failed"
        )

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/pair/status")
    async def pair_status(request: Request) -> JSONResponse:
        if not app.state.accepting:
            raise HTTPException(503)
        proof = request.cookies.get(PENDING)
        if proof:
            return paired(auth.status(proof))
        csrf = secrets.token_urlsafe(32)
        response = JSONResponse({"status": "unpaired", "csrf_token": csrf})
        cookie(response, BOOTSTRAP, csrf, 120)
        return response

    @app.post("/api/v1/pair")
    async def pair(request: Request, body: PairRequest) -> JSONResponse:
        if not app.state.accepting:
            raise HTTPException(503)
        csrf = request.cookies.get(BOOTSTRAP, "")
        if not csrf or not hmac.compare_digest(
            csrf.encode(), request.headers.get("x-csrf-token", "").encode()
        ):
            raise HTTPException(403)
        if bool(body.ticket) == bool(body.code):
            raise HTTPException(400)
        return paired(auth.pair(ticket=body.ticket, code=body.code, name=body.name))

    @app.post("/api/v1/logout")
    async def logout(request: Request) -> JSONResponse:
        d = device(request, write=True, unverified=True)
        auth.revoke(d.id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/v1/session")
    async def session(request: Request) -> dict[str, Any]:
        d = device(request, unverified=True)
        csrf = auth.csrf_token(request.cookies[COOKIE])
        return {
            "device_id": d.id,
            "server_epoch": facade.epoch,
            "csrf_token": csrf,
            "password_required": password_gate is not None and not d.password_verified,
            "transport_secure": config.tls,
            "permissions": {"chat": password_gate is None or d.password_verified, "tools": False},
        }

    @app.get("/api/v1/password/challenge")
    async def password_challenge(request: Request) -> dict[str, object]:
        d = device(request, unverified=True)
        if password_gate is None:
            raise HTTPException(404)
        return password_gate.challenge(d)

    @app.post("/api/v1/password/verify")
    async def verify_password(request: Request, body: PasswordEnvelope) -> dict[str, bool]:
        d = device(request, write=True, unverified=True)
        if password_gate is None:
            raise HTTPException(404)
        try:
            accepted = await password_gate.verify(auth, d, **body.model_dump())
        except AuthError:
            raise PasswordError() from None
        if not accepted:
            raise PasswordError()
        return {"ok": True}

    @app.get("/api/v1/state")
    async def state(request: Request) -> dict[str, Any]:
        device(request)
        value = await facade.state()
        if not value.get("available", True):
            auth.invalidate()
            raise HTTPException(503)
        return project(value, "state")

    @app.get("/api/v1/models")
    async def models(request: Request) -> list[dict[str, Any]]:
        device(request)
        return [project(m, "model") for m in await facade.models()]

    def pagination(request: Request) -> tuple[int, str | None]:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            raise HTTPException(400) from None
        cursor = request.query_params.get("cursor")
        if not 1 <= limit <= 100 or (cursor and len(cursor) > 512):
            raise HTTPException(400)
        return limit, cursor

    @app.get("/api/v1/conversations")
    async def conversations(request: Request) -> dict[str, Any]:
        device(request)
        limit, cursor = pagination(request)
        return page(await facade.conversations(limit=limit, cursor=cursor), "conversation")

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    async def messages(request: Request, conversation_id: str) -> dict[str, Any]:
        device(request)
        limit, cursor = pagination(request)
        branch = request.query_params.get("branch_id")
        if not branch or len(branch) > 160 or len(conversation_id) > 160:
            raise HTTPException(400)
        return page(
            await facade.messages(conversation_id, branch, limit=limit, cursor=cursor), "message"
        )

    @app.post("/api/v1/commands")
    async def commands(request: Request) -> JSONResponse:
        d = device(request, write=True)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400) from None
        command: Any = TypeAdapter(Command).validate_python(payload)
        result = await facade.execute(d.id, command.model_dump(exclude_none=True))
        if result.get("status") in ("rejected", "failed"):
            error = safe_error(result.get("error"))
            return error_response(error["status"], error["code"])
        return JSONResponse(project(result, "receipt"))

    @app.get("/api/v1/commands/{command_id}")
    async def receipt(request: Request, command_id: str) -> dict[str, Any]:
        d = device(request)
        if len(command_id) > 160:
            raise HTTPException(400)
        result = facade.receipt(d.id, command_id)
        if result is None:
            raise HTTPException(404)
        return project(result, "receipt")

    @app.get("/api/v1/events")
    async def events(request: Request) -> StreamingResponse:
        d = device(request)
        cursor = request.headers.get("last-event-id")
        if cursor is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}:[0-9]{1,20}", cursor):
            raise HTTPException(400)
        if not (await facade.state()).get("available", True):
            auth.invalidate()
            raise HTTPException(503)
        if app.state.streams >= config.max_streams:
            raise HTTPException(429)
        app.state.streams += 1

        def closed() -> None:
            app.state.streams -= 1

        return RevocableEventResponse(
            event_stream(facade, auth, d, cursor, config.max_event_bytes), auth, d, closed
        )

    static = Path(__file__).resolve().parent / "static"

    @app.get("/{asset_path:path}")
    async def assets(asset_path: str) -> FileResponse:
        if asset_path.startswith("api/") or asset_path in ("api", "docs", "redoc", "openapi.json"):
            raise HTTPException(404)
        target = (static / (asset_path or "index.html")).resolve()
        if not target.is_relative_to(static.resolve()) or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    return app
