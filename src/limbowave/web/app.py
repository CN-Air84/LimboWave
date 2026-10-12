"""Authenticated same-origin API and packaged static assets."""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException

from limbowave.application.services.command_receipts import RuntimeConflict

from .auth import AuthError, AuthStore, Device
from .dto import page, project, safe_error
from .password_gate import PasswordError, PasswordGate
from .schemas import Command, MemoryMutation, PairRequest, PasswordEnvelope, SessionMemoryMutation
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
    *,
    locked: Callable[[], bool] | None = None,
) -> FastAPI:
    if not ipaddress.ip_address(config.host).is_loopback and password_gate is None:
        raise ValueError("局域网访问必须配置资料库密码核验")
    auth = auth or AuthStore()
    is_locked = locked or (lambda: False)

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
            name,
            value,
            max_age=age,
            httponly=True,
            secure=config.transport_secure,
            samesite="strict",
            path="/",
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
        if is_locked():
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
        return {"ok": True, "locked": is_locked()}

    @app.get("/api/v1/pair/status")
    async def pair_status(request: Request) -> JSONResponse:
        if not app.state.accepting:
            raise HTTPException(503)
        if is_locked():
            return JSONResponse({"status": "locked"})
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
        if is_locked():
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
            "transport_secure": config.transport_secure,
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

    def memory_service() -> Any:
        service = getattr(facade, "memory_service", None)
        if service is None:
            raise HTTPException(503)
        return service

    def memory_revision() -> int:
        return int(getattr(facade, "revision", 0))

    def check_memory_revision(expected: int) -> None:
        if expected != memory_revision():
            raise RuntimeConflict("memory_conflict", 409)

    def memory_item(value: Any) -> dict[str, Any]:
        return project(value, "memory")

    @app.get("/api/v1/memories")
    async def memories(request: Request) -> dict[str, Any]:
        device(request)
        conversation_id = request.query_params.get("conversation_id")
        branch_id = request.query_params.get("branch_id")
        if not conversation_id or not branch_id:
            raise HTTPException(400)
        service = memory_service()
        try:
            session = service.list_session_global(conversation_id)
            branch = service.list(conversation_id, branch_id)
            return {
                "revision": memory_revision(),
                "global": [memory_item(item) for item in service.list()],
                "session_global": [
                    {"memory": memory_item(item.memory), "state": item.state} for item in session
                ],
                "branch": [memory_item(item) for item in branch],
                "policy": service.policy(conversation_id).value,
                "settings": service.settings().model_dump(),
            }
        except ValueError:
            raise HTTPException(404) from None

    @app.post("/api/v1/memories")
    async def save_memory(request: Request, body: MemoryMutation) -> JSONResponse:
        device(request, write=True)
        check_memory_revision(body.expected_revision)
        service = memory_service()
        try:
            if body.policy is not None:
                if not body.conversation_id:
                    raise ValueError("conversation required")
                service.set_policy(body.conversation_id, body.policy)
                result: dict[str, Any] = {"policy": body.policy}
            elif (
                body.conversation_id
                and body.branch_id
                and body.item_id
                and body.content is not None
            ):
                result = memory_item(
                    service.save(
                        body.content,
                        body.conversation_id,
                        body.branch_id,
                        item_id=body.item_id,
                    )
                )
            elif (
                body.conversation_id is None
                and body.branch_id is None
                and body.content is not None
            ):
                result = memory_item(service.save(body.content))
            elif body.conversation_id and body.branch_id and body.content is not None:
                result = memory_item(
                    service.save(body.content, body.conversation_id, body.branch_id)
                )
            else:
                raise ValueError("invalid memory scope")
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision(), "item": result})
        except ValueError as exc:
            code = (
                "memory_invalid_scope"
                if "scope" in str(exc) or "会话" in str(exc)
                else "memory_not_found"
            )
            return error_response(400 if code == "memory_invalid_scope" else 404, code)

    @app.post("/api/v1/memories/session")
    async def save_session_memory(request: Request, body: SessionMemoryMutation) -> JSONResponse:
        device(request, write=True)
        check_memory_revision(body.expected_revision)
        try:
            service = memory_service()
            if body.content is None:
                raise ValueError("content required")
            # Session-global writes must reference a real global ID.
            conversations = await facade.conversations(limit=100)
            if body.conversation_id not in {c["id"] for c in conversations["items"]}:
                raise ValueError("conversation not found")
            if body.content is not None:
                service.save_session_global(body.item_id, body.content, body.conversation_id)
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision()})
        except ValueError:
            return error_response(404, "memory_not_found")

    @app.post("/api/v1/memories/{item_id}/delete")
    async def delete_memory(request: Request, item_id: str) -> JSONResponse:
        device(request, write=True)
        try:
            payload = MemoryMutation.model_validate(await request.json())
            check_memory_revision(payload.expected_revision)
            if not payload.conversation_id or not payload.branch_id:
                raise ValueError("scope")
            memory_service().delete(item_id, payload.conversation_id, payload.branch_id)
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision()})
        except ValueError:
            return error_response(404, "memory_not_found")

    @app.post("/api/v1/memories/{item_id}/promote")
    async def promote_memory(request: Request, item_id: str) -> JSONResponse:
        device(request, write=True)
        try:
            payload = MemoryMutation.model_validate(await request.json())
            check_memory_revision(payload.expected_revision)
            if not payload.conversation_id or not payload.branch_id:
                raise ValueError("scope")
            item = memory_service().promote(item_id, payload.conversation_id, payload.branch_id)
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision(), "item": memory_item(item)})
        except ValueError:
            return error_response(404, "memory_not_found")

    @app.post("/api/v1/memories/session/{item_id}/delete")
    async def delete_session_memory(request: Request, item_id: str) -> JSONResponse:
        device(request, write=True)
        try:
            payload = MemoryMutation.model_validate(await request.json())
            check_memory_revision(payload.expected_revision)
            if not payload.conversation_id:
                raise ValueError("conversation")
            memory_service().delete_session_global(item_id, payload.conversation_id)
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision()})
        except ValueError:
            return error_response(404, "memory_not_found")

    @app.post("/api/v1/memories/session/{item_id}/restore")
    async def restore_session_memory(request: Request, item_id: str) -> JSONResponse:
        device(request, write=True)
        try:
            payload = MemoryMutation.model_validate(await request.json())
            check_memory_revision(payload.expected_revision)
            if not payload.conversation_id:
                raise ValueError("conversation")
            memory_service().restore_session_global(item_id, payload.conversation_id)
            facade.bump_revision()
            return JSONResponse({"revision": memory_revision()})
        except ValueError:
            return error_response(404, "memory_not_found")

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
