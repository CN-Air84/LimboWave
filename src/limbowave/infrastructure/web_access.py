"""Bounded HTTP(S); public-only unless a trusted full-access invocation allows more."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext, suppress
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from limbowave.application.services.network_privacy import (
    check_network_destination,
    guard_network_request,
)
from limbowave.application.services.tool_access import has_full_access
from limbowave.domain.network import WebAccessError, WebResponse
from limbowave.domain.network_guard import resolve_host

PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain", "text/markdown"})
MAX_PAGE_BYTES = 2 * 1024 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_TRANSITION_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001::/32",
        "2002::/16",
    )
)


def _public_address(value: str) -> bool:
    if value == "168.63.129.16":  # Azure platform/WireServer endpoint, not a public website.
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _public_address(str(address.ipv4_mapped))
        if any(address in network for network in _TRANSITION_NETWORKS):
            return False
    return True


def validate_public_url(url: str) -> tuple[str, str, int]:
    """Validate syntax without DNS/network. Authorization happens before resolution."""
    full_access = has_full_access()
    try:
        if not url or len(url) > 4096 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError
        if "\\" in url or "%" in (urlsplit(url).hostname or ""):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.username is not None:
            raise ValueError
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        default_port = 443 if parts.scheme == "https" else 80
        if not host or port < 1 or (not full_access and port != default_port):
            raise ValueError
        if not full_access and (
            host in {"localhost", "metadata.goog", "metadata.google.internal"}
            or host.endswith((".localhost", ".local", ".internal", ".home"))
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(not (c.isascii() and (c.isalnum() or c == "-")) for c in label)
                for label in host.split(".")
            ):
                raise ValueError from None
        else:
            if not full_access and not _public_address(str(address)):
                raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        if port != default_port:
            netloc += f":{port}"
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        normalized = urlunsplit((parts.scheme, netloc, path, query, ""))
        if len(normalized) > 8192:
            raise ValueError
        return normalized, host, port
    except (ValueError, UnicodeError):
        raise WebAccessError(
            "blocked_url", ("仅允许合法且无内嵌凭据的 HTTP(S) 地址" if full_access else
                            "仅允许无内嵌凭据、标准端口的公网 HTTP(S) 地址")
        ) from None


def _open_socket(ips: list[str], port: int, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    timed_out = False
    for ip in ips:
        sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            sock.connect(
                (ip, port)
            )  # Numeric, previously checked IP: never resolve hostname again.
            return sock
        except OSError as exc:
            timed_out = timed_out or isinstance(exc, TimeoutError)
            sock.close()
    if timed_out:
        raise TimeoutError
    raise OSError("Public connection failed")


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ips = ips
        self._timeout = timeout

    def connect(self) -> None:
        self.sock = _open_socket(self._ips, self.port, self._timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)
        self._ips = ips
        self._timeout = timeout

    def connect(self) -> None:
        started = time.monotonic()
        raw = _open_socket(self._ips, self.port, self._timeout)
        try:
            remaining = self._timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            raw.settimeout(remaining)
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class PublicHTTPClient:
    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]] = resolve_host,
        timeout: float = 20.0,
        max_redirects: int = 3,
    ) -> None:
        self._resolver = resolver
        self._timeout = timeout
        self._max_redirects = max_redirects

    def _resolve(self, host: str, timeout: float) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return [host]
        if not _DNS_SLOTS.acquire(blocking=False):
            raise WebAccessError("network_busy", "公网 DNS 查询繁忙，请稍后重试")
        done = threading.Event()
        results: list[str] = []

        def worker() -> None:
            try:
                results.extend(self._resolver(host))
            except Exception:
                pass
            finally:
                _DNS_SLOTS.release()
                done.set()

        threading.Thread(target=worker, daemon=True, name="public-web-dns").start()
        if not done.wait(max(0.0, timeout)):
            raise WebAccessError("timeout", "网页请求超时（DNS），未取得可用内容")
        if not results:
            raise WebAccessError("dns_failed", "公网主机无法解析")
        return results

    @staticmethod
    def _connection(
        scheme: str,
        host: str,
        port: int,
        ips: list[str],
        timeout: float,
    ) -> http.client.HTTPConnection:
        cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
        return cls(host, port, ips, timeout)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        max_bytes: int = MAX_PAGE_BYTES,
        accepted_types: frozenset[str] = PAGE_TYPES,
        allow_redirects: bool = True,
        check_url: Callable[[str], None] | None = None,
    ) -> WebResponse:
        """Check invocation guards before every DNS lookup, including redirects.

        check_url is an optional additional synchronous check (None means allow;
        raise means deny), not an override of invocation-scoped privacy checks.
        No checker is retained on this reusable client or sent to DNS workers.
        """
        if method not in {"GET", "POST"} or max_bytes <= 0:
            raise WebAccessError("invalid_request", "不支持的网络请求")
        full_access = has_full_access()
        request_overrides = dict(headers or {})
        deadline = time.monotonic() + self._timeout
        current, host, port = validate_public_url(url)

        def check_destination(destination: str) -> None:
            with guard_network_request(check_url) if check_url is not None else nullcontext():
                check_network_destination(destination)

        for hop in range(self._max_redirects + 1):
            check_destination(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebAccessError("timeout", "网页请求超时，未取得可用内容")
            ips = self._resolve(host, remaining)
            if not full_access and not all(_public_address(ip) for ip in ips):
                raise WebAccessError("blocked_address", "目标包含非公网地址，已阻止请求")
            parts = urlsplit(current)
            conn = None
            response: http.client.HTTPResponse | None = None
            watchdog: threading.Timer | None = None
            expired = threading.Event()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                conn = self._connection(parts.scheme, host, port, ips, remaining)
                request_headers = {
                    "User-Agent": "LimboWave/NET-01 (read-only web access)",
                    "Accept": ", ".join(sorted(accepted_types)),
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    **request_overrides,
                }
                target = parts.path + ("?" + parts.query if parts.query else "")
                conn.request(method, target, body=body, headers=request_headers)
                active_socket = conn.sock  # HTTPConnection may clear .sock after headers.

                def expire(
                    sock: socket.socket | None = active_socket,
                    event: threading.Event = expired,
                ) -> None:
                    event.set()
                    if sock is not None:
                        with suppress(OSError):
                            sock.shutdown(socket.SHUT_RDWR)
                        sock.close()

                watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
                watchdog.daemon = True
                watchdog.start()
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not allow_redirects or not location:
                        raise WebAccessError(
                            "redirect_blocked", "搜索服务重定向已阻止；凭据不会转发"
                        )
                    next_url, next_host, next_port = validate_public_url(urljoin(current, location))
                    if (urlsplit(next_url).scheme, next_host, next_port) != (
                        parts.scheme,
                        host,
                        port,
                    ):
                        # Not requested automatically, but do not return a prohibited
                        # target in structured errors for another tool call either.
                        check_destination(next_url)
                        if not full_access or method != "GET":
                            raise WebAccessError(
                                "redirect_requires_permission",
                                "网页重定向到另一来源；需单独授权 read_url",
                                data={"redirect_url": next_url},
                            )
                        # Authorization is broad, but credentials belong to their origin.
                        request_overrides = {
                            key: value for key, value in request_overrides.items()
                            if key.lower() not in {"authorization", "proxy-authorization",
                                                   "cookie", "host"}
                        }
                    if hop == self._max_redirects:
                        raise WebAccessError("redirect_limit", "网页重定向次数超过上限")
                    current, host, port = next_url, next_host, next_port
                    continue
                if not 200 <= response.status < 300:
                    raise WebAccessError(
                        "http_status", "远端未返回成功响应", data={"status": response.status}
                    )
                content_type = response.getheader("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() not in accepted_types:
                    raise WebAccessError("unsupported_content_type", "响应不是支持的只读文本类型")
                if response.getheader("Content-Encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise WebAccessError(
                        "unsupported_encoding", "不接受压缩响应，以避免解压资源耗尽"
                    )
                chunks: list[bytes] = []
                total = 0
                while total <= max_bytes:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if active_socket is not None:
                        active_socket.settimeout(remaining)
                    chunk = response.read1(min(65536, max_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError
                raw = b"".join(chunks)
                if len(raw) <= max_bytes and (getattr(response, "length", None) or 0) > 0:
                    raise WebAccessError("incomplete_response", "网页连接提前结束，未取得完整响应")
                return WebResponse(
                    current,
                    response.status,
                    content_type[:512],
                    raw[:max_bytes],
                    datetime.now(UTC).isoformat(),
                    len(raw) > max_bytes,
                )
            except WebAccessError:
                raise
            except TimeoutError:
                raise WebAccessError("timeout", "网页请求超时，未取得可用内容") from None
            except ssl.SSLError:
                raise WebAccessError("tls_error", "HTTPS 证书或安全连接校验失败") from None
            except (OSError, http.client.HTTPException, ValueError):
                if expired.is_set() or time.monotonic() >= deadline:
                    raise WebAccessError("timeout", "网页请求超时，未取得可用内容") from None
                raise WebAccessError("connection_failed", "公网请求失败，未取得可用内容") from None
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                if response is not None:
                    response.close()
                if conn is not None:
                    conn.close()
        raise WebAccessError("redirect_limit", "网页重定向次数超过上限")
