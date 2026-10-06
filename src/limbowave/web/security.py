"""Strict LAN binding and bounded ASGI ingress."""

from __future__ import annotations

import ipaddress
import secrets
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    certfile: str | None = None
    keyfile: str | None = None
    allow_insecure_http: bool = False
    max_body: int = 65536
    max_requests: int = 32
    max_streams: int = 8
    max_event_bytes: int = 262144
    request_timeout: float = 15
    requests_per_minute: int = 240

    @property
    def tls(self) -> bool:
        return bool(self.certfile and self.keyfile)

    @property
    def authority(self) -> str:
        default_port = 443 if self.tls else 80
        return self.host if self.port == default_port else f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.authority}"

    def validate(self) -> None:
        try:
            ip = ipaddress.IPv4Address(self.host)
        except ValueError:
            raise ValueError("A concrete private IPv4 address is required") from None
        private = any(
            ip in ipaddress.IPv4Network(net)
            for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        if not (ip.is_loopback or private) or ip.is_unspecified or ip.is_multicast:
            raise ValueError("A concrete private IPv4 address is required")
        if not 0 <= self.port <= 65535:
            raise ValueError("Invalid port")
        if not ip.is_loopback and not self.tls and not self.allow_insecure_http:
            raise ValueError("局域网未配置 HTTPS/TLS：请选择证书和私钥，或明确勾选 HTTP 风险确认")
        if bool(self.certfile) != bool(self.keyfile):
            raise ValueError("Both TLS certificate and key are required")
        if (
            min(
                self.max_body,
                self.max_requests,
                self.max_streams,
                self.max_event_bytes,
                self.requests_per_minute,
                self.request_timeout,
            )
            <= 0
        ):
            raise ValueError("Limits must be positive")
        if self.tls:
            assert self.certfile is not None
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(self.certfile, self.keyfile)
                # Validate IP SAN and validity, without assuming this process trusts the local CA.
                from datetime import UTC, datetime

                from cryptography import x509

                cert = x509.load_pem_x509_certificate(Path(self.certfile).read_bytes())
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                if ip not in san.get_values_for_type(x509.IPAddress):
                    raise ValueError()
                now = datetime.now(UTC)
                if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
                    raise ValueError()
            except Exception:
                raise ValueError("Invalid TLS certificate/key or IP SAN/validity") from None


def error_response(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {
            "code": code,
            "message": code.replace("_", " "),
            "request_id": secrets.token_hex(8),
            "retryable": status in (429, 503),
        },
        status_code=status,
    )


class SecurityMiddleware:
    def __init__(self, app: Any, *, config: WebConfig) -> None:
        self.app, self.config = app, config
        self.active = 0
        self.window = time.monotonic()
        self.count = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        c = self.config
        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.lower(), []).append(value)
        started = False
        denied = False

        async def safe_send(message: Any) -> None:
            nonlocal started, denied
            if denied:
                return
            device = scope.get("web_device")
            auth = scope.get("web_auth")
            if (
                device is not None
                and message["type"] == "http.response.start"
                and message["status"] >= 400
            ):
                # Sanitized error responses contain no runtime data and keep their classification.
                scope.pop("web_device", None)
                device = None
            if device is not None and auth is not None and not auth.valid(device):
                if not started:
                    # Handler may have awaited I/O while the desktop revoked this device.
                    # Logout intentionally revokes itself and may still acknowledge it.
                    if scope["path"] != "/api/v1/logout":
                        scope.pop("web_device", None)
                        await error_response(401, "unauthorized")(scope, receive, safe_send)
                        denied = True
                        return
                elif scope["path"] != "/api/v1/logout":
                    denied = True
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
            if message["type"] == "http.response.start":
                started = True
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; "
                        b"style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                        b"object-src 'none'; base-uri 'none'; "
                        b"frame-ancestors 'none'; form-action 'self'",
                    ),
                ]
            await send(message)

        async def reject(status: int, code: str) -> None:
            await error_response(status, code)(scope, receive, safe_send)

        if headers.get(b"host") != [c.authority.encode()]:
            await reject(400, "invalid_host")
            return
        origin = headers.get(b"origin")
        if (origin is not None and origin != [c.origin.encode()]) or (
            scope["method"] not in ("GET", "HEAD") and origin != [c.origin.encode()]
        ):
            await reject(403, "invalid_origin")
            return
        if headers.get(b"sec-fetch-site", [b"same-origin"]) not in ([b"same-origin"], [b"none"]):
            await reject(403, "cross_site_request")
            return
        if scope.get("query_string") and scope["path"] in (
            "/api/v1/pair",
            "/api/v1/pair/status",
            "/api/v1/events",
        ):
            await reject(400, "invalid_query")
            return
        if time.monotonic() - self.window >= 60:
            self.window, self.count = time.monotonic(), 0
        self.count += 1
        if self.count > c.requests_per_minute or self.active >= c.max_requests:
            await reject(429, "request_limit")
            return
        if len(headers.get(b"content-length", [])) > 1:
            await reject(400, "invalid_length")
            return
        try:
            length = int(headers.get(b"content-length", [b"0"])[0])
        except ValueError:
            length = -1
        if length < 0 or length > c.max_body:
            await reject(413, "body_limit")
            return
        self.active += 1
        try:
            # Buffer only bounded bodies before invoking a handler: no partial side effects.
            import asyncio

            body = bytearray()
            async with asyncio.timeout(c.request_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > c.max_body:
                        await reject(413, "body_limit")
                        return
                    if not message.get("more_body", False):
                        break
            consumed = False

            async def replay() -> Any:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, safe_send)
        except TimeoutError:
            if not started:
                await reject(408, "request_timeout")
        except Exception:
            if not started:
                await reject(500, "internal_error")
        finally:
            self.active -= 1
