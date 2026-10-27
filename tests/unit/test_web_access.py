"""NET-01 transport tests never require live network access."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from limbowave.application.services.web_content import extract_page
from limbowave.domain.network import WebAccessError, WebResponse
from limbowave.infrastructure.web_access import PublicHTTPClient, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://user:pass@example.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://[64:ff9b::a00:1]/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "http://example.com:22/",
        "https://example.com/\\evil",
        "https://example.com/\nX:foo",
    ],
)
def test_unsafe_urls_rejected_without_network(url):
    with pytest.raises(WebAccessError):
        validate_public_url(url)


def test_canonical_url_keeps_query_and_removes_fragment():
    assert validate_public_url("HTTPS://Example.com/a?q=x#part")[0] == "https://example.com/a?q=x"


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=b"hello", headers=None, status=200):
        super().__init__(body)
        self.headers = {"Content-Type": "text/plain", **(headers or {})}
        self.status = status

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = Mock()
        self.requests = []
        self.closed = False

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def client_with(monkeypatch, responses, addresses=None):
    calls = []
    connections = []

    def connect(scheme, host, port, ips, timeout):
        calls.append((scheme, host, port, ips))
        conn = Connection(responses.pop(0))
        connections.append(conn)
        return conn

    client = PublicHTTPClient(resolver=lambda host: addresses or ["93.184.216.34"])
    monkeypatch.setattr(client, "_connection", connect)
    return client, calls, connections


def test_pins_validated_address_and_does_not_forward_ambient_auth(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    client, calls, conns = client_with(monkeypatch, [Response()])
    result = client.request("https://example.com/a?q=b")
    assert result.body == b"hello"
    assert calls == [("https", "example.com", 443, ["93.184.216.34"])]
    headers = conns[0].requests[0][3]
    assert "Cookie" not in headers and "Authorization" not in headers
    assert conns[0].closed and result.retrieved_at.endswith("+00:00")


def test_mixed_public_private_dns_is_denied_before_connect(monkeypatch):
    client, calls, _ = client_with(monkeypatch, [], ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(WebAccessError, match="公网"):
        client.request("https://example.com/")
    assert not calls


def test_redirect_cross_origin_requires_new_permission(monkeypatch):
    client, calls, _ = client_with(
        monkeypatch, [Response(headers={"Location": "https://other.example/a"}, status=302)]
    )
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "redirect_requires_permission"
    assert exc.value.data["redirect_url"] == "https://other.example/a"
    assert len(calls) == 1


def test_same_origin_redirect_reports_real_final_url(monkeypatch):
    client, calls, _ = client_with(
        monkeypatch, [Response(headers={"Location": "/article"}, status=301), Response()]
    )
    assert client.request("https://example.com/start").url == "https://example.com/article"
    assert len(calls) == 2


def test_redirect_to_private_never_connects(monkeypatch):
    client, calls, _ = client_with(
        monkeypatch, [Response(headers={"Location": "http://127.0.0.1/"}, status=302)]
    )
    with pytest.raises(WebAccessError):
        client.request("https://example.com/")
    assert len(calls) == 1


def test_bounded_body_and_truthful_truncation(monkeypatch):
    client, _, _ = client_with(monkeypatch, [Response(b"a" * 100)])
    result = client.request("https://example.com/", max_bytes=12)
    assert result.body == b"a" * 12 and result.truncated


@pytest.mark.parametrize(
    "response,code",
    [
        (Response(status=403), "http_status"),
        (Response(headers={"Content-Type": "application/pdf"}), "unsupported_content_type"),
        (Response(headers={"Content-Encoding": "gzip"}), "unsupported_encoding"),
    ],
)
def test_response_failures_are_classified(monkeypatch, response, code):
    client, _, _ = client_with(monkeypatch, [response])
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == code


def test_timeout_error_does_not_leak_url_or_exception(monkeypatch):
    client = PublicHTTPClient(resolver=lambda host: ["93.184.216.34"])
    monkeypatch.setattr(client, "_connection", Mock(side_effect=TimeoutError("SECRET")))
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/?api_key=SECRET")
    assert exc.value.code == "timeout" and "SECRET" not in str(exc.value)


def test_html_is_cleaned_without_executing_or_fetching_resources():
    html = (
        "<html><head><title>News &amp; facts</title><style>bad-style</style></head>"
        "<body><nav>bad-nav</nav><main><h1>Headline</h1><p>Real article.</p>"
        "<script>bad-script</script><form>bad-form</form><div hidden>bad-hidden</div>"
        "<iframe>bad-frame</iframe><p>Next paragraph</p></main>"
        "<footer>bad-footer</footer></body></html>"
    )
    data, truncated = extract_page(
        WebResponse(
            "https://example.com/final",
            200,
            "text/html; charset=utf-8",
            html.encode(),
            "2026-10-27T00:00:00+00:00",
        )
    )
    assert data["title"] == "News & facts"
    assert "Real article." in data["text"] and "Next paragraph" in data["text"]
    assert "bad-" not in data["text"]
    assert data["url"].endswith("/final") and data["evidence_type"] == "page_content"
    assert data["retrieved_at"] == "2026-10-27T00:00:00+00:00" and not truncated


def test_plain_text_charset_and_utf8_byte_limit():
    result = WebResponse(
        "https://example.com/",
        200,
        "text/plain; charset=gb18030",
        "中文内容".encode("gb18030"),
        "now",
    )
    data, truncated = extract_page(result, max_bytes=6)
    assert data["text"] == "中文" and truncated


def test_empty_javascript_shell_is_not_claimed_as_read_article():
    with pytest.raises(WebAccessError) as exc:
        extract_page(
            WebResponse(
                "https://example.com/",
                200,
                "text/html",
                b"<html><script>render()</script></html>",
                "now",
            )
        )
    assert exc.value.code == "empty_content"


@pytest.mark.parametrize(
    "url", ["https://example.com:0/", "https://example.com:444/", "http://[2002:7f00:1::]/"]
)
def test_additional_unsafe_ports_and_transition_addresses(url):
    with pytest.raises(WebAccessError):
        validate_public_url(url)


def test_connection_uses_numeric_ip_and_tls_uses_original_hostname(monkeypatch):
    from limbowave.infrastructure import web_access

    socket_factory = Mock()
    sock = socket_factory.return_value
    monkeypatch.setattr(web_access.socket, "socket", socket_factory)
    monkeypatch.setattr(
        web_access.socket, "getaddrinfo", Mock(side_effect=AssertionError("DNS rebound"))
    )
    tls = Mock()
    monkeypatch.setattr(web_access.ssl, "create_default_context", lambda: tls)
    connection = web_access._PinnedHTTPS("example.com", 443, ["93.184.216.34"], 1)
    connection.connect()
    sock.connect.assert_called_once_with(("93.184.216.34", 443))
    tls.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")


def test_same_origin_redirect_rechecks_dns_and_rejects_rebinding(monkeypatch):
    responses = [Response(headers={"Location": "/next"}, status=302)]
    client, calls, _ = client_with(monkeypatch, responses)
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    client._resolver = lambda host: next(answers)
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "blocked_address" and len(calls) == 1


def test_redirect_limit_closes_every_connection(monkeypatch):
    responses = [Response(headers={"Location": "/next"}, status=302) for _ in range(4)]
    client, calls, connections = client_with(monkeypatch, responses)
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "redirect_limit" and len(calls) == 4
    assert all(c.closed and c.response.closed for c in connections)


def test_provider_redirect_never_forwards_credentials(monkeypatch):
    client, calls, _ = client_with(
        monkeypatch, [Response(headers={"Location": "https://other.example/"}, status=302)]
    )
    with pytest.raises(WebAccessError) as exc:
        client.request(
            "https://example.com/",
            headers={"Authorization": "Bearer SECRET"},
            allow_redirects=False,
        )
    assert exc.value.code == "redirect_blocked" and len(calls) == 1


def test_truncated_response_closed_promptly(monkeypatch):
    response = Response(b"x" * 100)
    client, _, connections = client_with(monkeypatch, [response])
    assert client.request("https://example.com/", max_bytes=10).truncated
    assert response.closed and connections[0].closed


def test_incomplete_content_length_is_failure_not_complete_evidence(monkeypatch):
    response = Response(b"partial")
    response.length = 100
    client, _, _ = client_with(monkeypatch, [response])
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "incomplete_response"


def test_dns_deadline_does_not_wait_for_slow_resolver():
    import time

    def resolve(host):
        time.sleep(0.1)
        return ["93.184.216.34"]

    client = PublicHTTPClient(resolver=resolve, timeout=0.01)
    started = time.monotonic()
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "timeout" and time.monotonic() - started < 0.08


def test_deadline_interrupts_header_trickle(monkeypatch):
    import time

    client, _, connections = client_with(monkeypatch, [Response()])
    client._timeout = 0.02
    original = client._connection

    def connection(*args):
        conn = original(*args)

        def getresponse():
            time.sleep(0.05)
            return conn.response

        conn.getresponse = getresponse
        return conn

    monkeypatch.setattr(client, "_connection", connection)
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "timeout"
    assert connections[0].sock.shutdown.called


def test_html_boolean_aria_attribute_and_charset_meta():
    html = '<meta charset="gb18030"><title>标题</title><p aria-hidden>可读正文</p>'
    data, _ = extract_page(
        WebResponse("https://example.com/", 200, "text/html", html.encode("gb18030"), "now")
    )
    assert data["title"] == "标题" and data["text"] == "可读正文"


@pytest.mark.parametrize("url", ["http://metadata.goog/", "http://168.63.129.16/"])
def test_platform_metadata_endpoints_are_not_public_websites(url):
    with pytest.raises(WebAccessError):
        validate_public_url(url)


def test_privacy_guard_rejects_initial_url_before_dns_or_connection(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    resolver = Mock(return_value=["93.184.216.34"])
    connect = Mock(side_effect=AssertionError("must not connect"))
    client = PublicHTTPClient(resolver=resolver)
    monkeypatch.setattr(client, "_connection", connect)

    def deny(url):
        raise RuntimeError("PRIVATE-DETAIL:" + url)

    with guard_network_request(deny), pytest.raises(WebAccessError) as exc:
        client.request("https://private-topic.example.com/?api_key=SECRET")
    assert exc.value.code == "privacy_denied"
    assert exc.value.data == {}
    assert all(word not in str(exc.value) for word in ["SECRET", "private-topic", "PRIVATE-DETAIL"])
    resolver.assert_not_called()
    connect.assert_not_called()


def test_privacy_guard_blocks_sensitive_same_origin_redirect_before_second_dns(monkeypatch):
    from urllib.parse import unquote

    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, connections = client_with(
        monkeypatch,
        [
            Response(headers={"Location": "/article?topic=%73ensitive"}, status=302),
        ],
    )
    resolver = Mock(return_value=["93.184.216.34"])
    client._resolver = resolver
    checked = []

    def check(url):
        checked.append(url)
        if "sensitive" in unquote(url):
            raise ValueError("SECRET-POLICY:" + url)

    with guard_network_request(check), pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/start")
    assert exc.value.code == "privacy_denied"
    assert len(checked) == 2 and checked[-1].endswith("topic=%73ensitive")
    assert resolver.call_count == 1 and len(calls) == 1
    assert connections[0].closed and connections[0].response.closed
    assert "sensitive" not in str(exc.value) and "SECRET" not in str(exc.value)


def test_allowed_redirects_still_work_and_every_hop_is_checked_before_dns(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, _ = client_with(
        monkeypatch,
        [
            Response(headers={"Location": "/article?q=ok"}, status=301),
            Response(),
        ],
    )
    order = []
    client._resolver = lambda host: order.append(("dns", host)) or ["93.184.216.34"]

    def check(url):
        order.append(("check", url))

    with guard_network_request(check):
        response = client.request("https://EXAMPLE.com/start#local")
    assert response.url == "https://example.com/article?q=ok" and len(calls) == 2
    assert order == [
        ("check", "https://example.com/start"),
        ("dns", "example.com"),
        ("check", "https://example.com/article?q=ok"),
        ("dns", "example.com"),
    ]


def test_per_request_check_adds_to_context_guard_not_replaces_it(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, _ = client_with(monkeypatch, [])
    client._resolver = Mock(side_effect=AssertionError("must not resolve"))

    def deny(url):
        raise ValueError(url)

    with guard_network_request(deny), pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/", check_url=lambda url: None)
    assert exc.value.code == "privacy_denied" and calls == []
    client._resolver.assert_not_called()


@pytest.mark.parametrize("return_value", [False, True, "allow"])
def test_optional_request_checker_non_none_results_fail_closed(monkeypatch, return_value):
    client, calls, _ = client_with(monkeypatch, [])
    client._resolver = Mock(side_effect=AssertionError("must not resolve"))
    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/", check_url=lambda url: return_value)
    assert exc.value.code == "privacy_denied" and calls == []
    client._resolver.assert_not_called()


def test_scoped_checker_does_not_leak_to_next_request_on_same_transport(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, _ = client_with(monkeypatch, [Response()])

    def deny(url):
        raise ValueError("PRIVATE")

    with pytest.raises(WebAccessError), guard_network_request(deny):
        client.request("https://example.com/")
    assert calls == []
    assert client.request("https://example.com/").status == 200 and len(calls) == 1


def test_scoped_allow_does_not_weaken_fake_ip_protection(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, _ = client_with(monkeypatch, [], ["198.18.0.1"])
    with guard_network_request(lambda url: None), pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/")
    assert exc.value.code == "blocked_address" and calls == []


def test_sensitive_cross_origin_redirect_is_not_returned_as_an_actionable_url(monkeypatch):
    from limbowave.application.services.network_privacy import guard_network_request

    client, calls, connections = client_with(
        monkeypatch,
        [
            Response(headers={"Location": "https://other.example/private-topic"}, status=302),
        ],
    )
    resolver = Mock(return_value=["93.184.216.34"])
    client._resolver = resolver

    def check(url):
        if "private-topic" in url:
            raise ValueError(url)

    with guard_network_request(check), pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/start")
    assert exc.value.code == "privacy_denied" and exc.value.data == {}
    assert "private-topic" not in str(exc.value)
    assert len(calls) == 1 and resolver.call_count == 1
    assert connections[0].closed and connections[0].response.closed


def test_optional_request_check_is_applied_to_every_normal_redirect(monkeypatch):
    client, calls, _ = client_with(
        monkeypatch,
        [
            Response(headers={"Location": "/article"}, status=302),
            Response(),
        ],
    )
    checked = []
    assert client.request("https://example.com/start", check_url=checked.append).status == 200
    assert checked == ["https://example.com/start", "https://example.com/article"]
    assert len(calls) == 2


def test_optional_request_checker_failure_is_redacted_and_reset(monkeypatch):
    client, calls, _ = client_with(monkeypatch, [Response()])

    def deny(url):
        raise ValueError("SECRET:" + url)

    with pytest.raises(WebAccessError) as exc:
        client.request("https://example.com/", check_url=deny)
    assert exc.value.code == "privacy_denied" and "SECRET" not in str(exc.value)
    assert calls == []
    assert client.request("https://example.com/").status == 200 and len(calls) == 1


@pytest.mark.parametrize("url,host,port", [
    ("http://127.0.0.1:8080/", "127.0.0.1", 8080),
    ("http://[::1]:9000/", "::1", 9000),
    ("http://10.0.0.1/", "10.0.0.1", 80),
    ("http://169.254.169.254/", "169.254.169.254", 80),
    ("http://localhost:3000/", "localhost", 3000),
    ("https://service.internal:8443/", "service.internal", 8443),
])
def test_full_access_allows_nonpublic_hosts_and_ports_without_leaking(monkeypatch, url, host, port):
    from limbowave.application.services.tool_access import tool_access_scope

    client, calls, _ = client_with(monkeypatch, [Response()], ["127.0.0.1"])
    with tool_access_scope(True):
        assert validate_public_url(url) == (url, host, port)
        assert client.request(url).body == b"hello"
    assert calls[0][1:3] == (host, port)
    with pytest.raises(WebAccessError):
        client.request(url)
    assert len(calls) == 1


def test_full_access_allows_fake_ip_dns_and_cross_origin_redirect(monkeypatch):
    from limbowave.application.services.tool_access import tool_access_scope

    client, calls, conns = client_with(monkeypatch, [
        Response(headers={"Location": "http://localhost:8080/article"}, status=302), Response(),
    ], ["198.18.0.10", "93.184.216.34"])
    with tool_access_scope(True):
        result = client.request("https://example.com/", headers={
            "Authorization": "test-only", "Cookie": "test-only", "X-Trace": "keep",
        })
    assert result.url == "http://localhost:8080/article"
    assert len(calls) == 2
    assert conns[1].requests[0][3]["X-Trace"] == "keep"
    assert "Authorization" not in conns[1].requests[0][3]
    assert "Cookie" not in conns[1].requests[0][3]
    with pytest.raises(WebAccessError, match="公网"):
        client.request("https://example.com/")
    assert len(calls) == 2


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:pass@example.com/", "http://example.com:0/",
    "http://example.com:65536/", "https://example.com/\\evil", "https://example.com/\nX:foo",
])
def test_full_access_preserves_url_syntax_validation(url):
    from limbowave.application.services.tool_access import tool_access_scope

    with tool_access_scope(True), pytest.raises(WebAccessError):
        validate_public_url(url)


async def test_full_access_context_isolated_and_reset_after_failure():
    import asyncio

    from limbowave.application.services.tool_access import has_full_access, tool_access_scope

    async def check(enabled):
        with tool_access_scope(enabled):
            await asyncio.sleep(0)
            assert has_full_access() is enabled
            with tool_access_scope(False):
                assert not has_full_access()
            assert has_full_access() is enabled
        assert not has_full_access()

    await asyncio.gather(check(True), check(False))
    with pytest.raises(RuntimeError), tool_access_scope(True):
        raise RuntimeError("simulated transport failure")
    assert not has_full_access()
