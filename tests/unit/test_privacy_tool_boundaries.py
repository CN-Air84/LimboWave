"""PRIV-02 regression reproductions for external inputs and structured outputs."""

from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway, ToolResult
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.permissions import PermissionPreset
from tests.unit.test_memory_service import memory_stack as memory_stack


@pytest.fixture
def boundary_stack(memory_stack, tmp_path):
    service, factory = memory_stack
    with factory() as uow:
        uow.messages.add(Message("private-source", "c", "b", MessageRole.USER,
                                 "EXACT_SOURCE_BODY", datetime.now(UTC)))
        uow.commit()
    service.privacy.add("FORBIDDEN_TOPIC", ["ＳＥＣＲＥＴＡＬＩＡＳ"], ["private-source"])
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    calls = []

    def search(query):
        calls.append(("search", query))
        return {"results": [], "count": 0}

    def fetch(url):
        calls.append(("fetch", url))
        return 200, "text/plain", b"safe page"

    gateway = ToolGateway(permissions, tmp_path, network=NetworkTools(
        searcher=search, fetcher=fetch, allow_private=True,
    ))
    gateway.memory_service = service
    return gateway, calls, service


@pytest.mark.parametrize("tool,params", [
    ("web_search", {"query": "FORBIDDEN_TOPIC"}),
    ("web_search", {"query": "secretalias"}),
    ("web_search", {"query": "EXACT_SOURCE_BODY"}),
    ("web_search", {"query": "private-source"}),
    ("read_url", {"url": "https://example.com/FORBIDDEN_TOPIC"}),
])
def test_forbidden_network_parameters_never_execute_even_confirmed(boundary_stack, tool, params):
    gateway, calls, _service = boundary_stack
    result = gateway.invoke(tool, params, "c", user_confirmed=True)
    assert calls == []
    assert not result.ok and result.data == {}
    assert "禁区" in result.error


def test_safe_network_request_still_uses_real_backend(boundary_stack):
    gateway, calls, _service = boundary_stack
    assert gateway.invoke("web_search", {"query": "safe query"}, "c", user_confirmed=True).ok
    assert calls == [("search", "safe query")]


@pytest.mark.parametrize("payload", [
    {"outer": [{"body": "EXACT_SOURCE_BODY"}]},
    {"nested": (("ＥＸＡＣＴ＿ＳＯＵＲＣＥ＿ＢＯＤＹ",),)},
    {"source_id": "private-source", "text": "opaque excerpt"},
    {"message_ids": ["private-source"]},
    {"FORBIDDEN_TOPIC": "safe value"},
])
def test_nested_source_content_or_id_rejects_whole_result(boundary_stack, monkeypatch, payload):
    gateway, _calls, _service = boundary_stack
    original = ToolResult(ok=True, data=payload)
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: original)
    result = gateway.invoke("read_url", {"url": "https://example.com"}, "c")
    assert not result.ok and result.data == {}
    assert original.data is payload  # Not a partial field deletion/redaction.


@pytest.mark.parametrize("original", [
    ToolResult(ok=False, error="EXACT_SOURCE_BODY"),
    ToolResult(ok=True, notes=("private-source",)),
])
def test_error_and_note_fields_are_checked(boundary_stack, monkeypatch, original):
    gateway, _calls, _service = boundary_stack
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: original)
    result = gateway.invoke("web_search", {"query": "safe"}, "c")
    assert not result.ok and result.data == {}
    assert "EXACT_SOURCE_BODY" not in str(result) and "private-source" not in str(result)


@pytest.mark.parametrize("kind", ["cycle", "depth", "unsupported", "bad_key"])
def test_uninspectable_result_fails_closed(boundary_stack, monkeypatch, kind):
    gateway, _calls, _service = boundary_stack
    if kind == "cycle":
        payload = {}
        payload["cycle"] = payload
    elif kind == "depth":
        payload = {}
        node = payload
        for _ in range(100):
            node["next"] = {}
            node = node["next"]
    elif kind == "bad_key":
        payload = {object(): "safe"}
    else:
        payload = {"unknown": object()}
    monkeypatch.setattr(
        gateway, "_invoke", lambda *args, **kwargs: ToolResult(ok=True, data=payload),
    )
    result = gateway.invoke("read_url", {"url": "https://example.com"}, "c")
    assert not result.ok and result.data == {}
    assert "隐私" in result.error


def test_malformed_policy_stops_network_before_backend(boundary_stack):
    from limbowave.application.services.privacy_service import PRIVACY_KEY
    from limbowave.domain.memory import MemoryDocument

    gateway, calls, service = boundary_stack
    with service._uow_factory() as uow:
        uow.memories.put(MemoryDocument(PRIVACY_KEY, '{"version":999}'))
        uow.commit()
    result = gateway.invoke("web_search", {"query": "safe"}, "c", user_confirmed=True)
    assert not result.ok and calls == []


def test_shared_references_and_plain_scalars_are_not_cycles(boundary_stack, monkeypatch):
    gateway, _calls, _service = boundary_stack
    shared = {"text": "safe"}
    original = ToolResult(ok=True, data={"rows": [shared, shared, None, True, 1, 2.5]})
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: original)
    assert gateway.invoke("read_url", {"url": "https://example.com"}, "c") is original


@pytest.mark.parametrize("payload", [
    {"bad": float("nan")}, {"bad": float("inf")}, {"bad": "\ud800"},
    {"wide": [None] * 20_001}, {"long": "x" * 2_000_001},
])
def test_non_json_or_over_budget_payload_rejected(boundary_stack, monkeypatch, payload):
    gateway, _calls, _service = boundary_stack
    monkeypatch.setattr(
        gateway, "_invoke", lambda *args, **kwargs: ToolResult(ok=True, data=payload),
    )
    result = gateway.invoke("read_url", {"url": "https://example.com"}, "c")
    assert not result.ok and result.data == {} and "隐私" in result.error


def test_custom_payload_does_not_get_stringified(boundary_stack, monkeypatch):
    gateway, _calls, _service = boundary_stack

    class Unsafe:
        def __str__(self):
            pytest.fail("custom stringification must not execute")

    original = ToolResult(ok=True, data={"value": Unsafe()})
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: original)
    assert not gateway.invoke("read_url", {"url": "https://example.com"}, "c").ok


@pytest.mark.parametrize("url", [
    "https://example.com/%46%4f%52%42%49%44%44%45%4e%5f%54%4f%50%49%43",
    "https://example.com/?q=%2546ORBIDDEN_TOPIC",
    "https://example.com/?q=FORBIDDEN+TOPIC",
    "https://example.com/?q=EXACT_SOURCE_BODY",
    "https://example.com/private-source",
])
def test_encoded_network_views_do_not_bypass_boundaries(boundary_stack, url):
    gateway, calls, service = boundary_stack
    service.privacy.add("forbidden topic")
    result = gateway.invoke("read_url", {"url": url}, "c", user_confirmed=True)
    assert not result.ok and calls == []


def test_idna_hostname_view_is_checked_without_changing_target(boundary_stack):
    gateway, calls, service = boundary_stack
    service.privacy.add("例子")
    result = gateway.invoke("read_url", {"url": "https://xn--fsqu00a.com/safe"}, "c",
                            user_confirmed=True)
    assert not result.ok and calls == []


@pytest.mark.parametrize("params,topic", [
    ({"location": "FORBIDDEN_TOPIC"}, "FORBIDDEN_TOPIC"),
    ({"latitude": 31.2304, "longitude": 121.4737}, "31.2304"),
])
def test_environment_network_params_are_checked_before_service(boundary_stack, monkeypatch,
                                                               params, topic):
    gateway, calls, service = boundary_stack
    if topic != "FORBIDDEN_TOPIC":
        service.privacy.add(topic)
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: calls.append("executed"))
    result = gateway.invoke("get_environment", params, "c", user_confirmed=True)
    assert not result.ok and calls == []


def test_safe_encoded_url_is_not_rewritten(boundary_stack):
    gateway, calls, _service = boundary_stack
    url = "https://example.com/safe%20path?q=public+news"
    assert gateway.invoke("read_url", {"url": url}, "c", user_confirmed=True).ok
    assert calls == [("fetch", url)]


def test_encoded_redirect_url_in_result_is_rejected(boundary_stack, monkeypatch):
    gateway, _calls, _service = boundary_stack
    original = ToolResult(ok=True, data={
        "nested": [{"redirect_url": "https://example.com/%46ORBIDDEN_TOPIC"}],
    })
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: original)
    assert not gateway.invoke("web_search", {"query": "safe"}, "c").ok


def test_gateway_installs_redirect_scope_and_resets_after_return(boundary_stack):
    from limbowave.application.services.network_privacy import check_network_destination

    gateway, calls, _service = boundary_stack

    def redirecting_fetch(url):
        check_network_destination(url)
        calls.append(url)
        redirect = "https://example.com/%46ORBIDDEN_TOPIC?api_key=DO_NOT_ECHO"
        check_network_destination(redirect)
        calls.append(redirect)
        return 200, "text/plain", b"safe"

    gateway._network = NetworkTools(fetcher=redirecting_fetch, allow_private=True)
    result = gateway.invoke("read_url", {"url": "https://example.com/safe"}, "c",
                            user_confirmed=True)
    assert not result.ok
    assert calls == ["https://example.com/safe"]
    assert "DO_NOT_ECHO" not in str(result) and "FORBIDDEN_TOPIC" not in str(result)
    check_network_destination("https://example.com/FORBIDDEN_TOPIC")  # Scope was reset.


def test_nested_network_scope_cannot_weaken_enclosing_guard(boundary_stack):
    from limbowave.application.services.network_privacy import (
        check_network_destination,
        guard_network_request,
    )
    from limbowave.domain.network import WebAccessError

    gateway, calls, _service = boundary_stack

    def outer_guard(url):
        raise WebAccessError("privacy_denied", "outer restriction")

    def fetch(url):
        check_network_destination(url)
        calls.append(url)
        return 200, "text/plain", b"safe"

    gateway._network = NetworkTools(fetcher=fetch, allow_private=True)
    with guard_network_request(outer_guard):
        assert not gateway.invoke("read_url", {"url": "https://example.com/safe"}, "c",
                                  user_confirmed=True).ok
        with pytest.raises(WebAccessError):
            check_network_destination("https://example.com/safe")
    assert calls == []


@pytest.mark.parametrize("location", [
    "/%46ORBIDDEN_TOPIC?api_key=DO_NOT_ECHO",
    "https://other.example/%46ORBIDDEN_TOPIC?api_key=DO_NOT_ECHO",
])
def test_real_transport_gateway_blocks_redirect_before_second_dns(boundary_stack, monkeypatch,
                                                                   location):
    from unittest.mock import Mock

    from limbowave.infrastructure.web_access import PublicHTTPClient
    from tests.unit.test_web_access import Connection, Response

    gateway, _calls, _service = boundary_stack
    resolver = Mock(return_value=["93.184.216.34"])
    client = PublicHTTPClient(resolver=resolver)
    response = Response(headers={"Location": location}, status=302)
    connection = Connection(response)
    connect = Mock(return_value=connection)
    monkeypatch.setattr(client, "_connection", connect)
    gateway._network = NetworkTools(page_fetcher=client.request)
    result = gateway.invoke("read_url", {"url": "https://example.com/start"}, "c",
                            user_confirmed=True)
    assert not result.ok and result.data.get("code") == "privacy_denied"
    assert resolver.call_count == connect.call_count == 1
    assert connection.closed and response.closed
    assert "DO_NOT_ECHO" not in str(result) and "FORBIDDEN_TOPIC" not in str(result)


def test_denied_initial_url_never_resolves_or_connects(boundary_stack, monkeypatch):
    from unittest.mock import Mock

    from limbowave.infrastructure.web_access import PublicHTTPClient

    gateway, _calls, _service = boundary_stack
    resolver, connect = Mock(), Mock()
    client = PublicHTTPClient(resolver=resolver)
    monkeypatch.setattr(client, "_connection", connect)
    gateway._network = NetworkTools(page_fetcher=client.request)
    assert not gateway.invoke("read_url", {"url": "https://example.com/%46ORBIDDEN_TOPIC"},
                              "c", user_confirmed=True).ok
    resolver.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"query": "%2546ORBIDDEN_TOPIC"},
    {"query": "%252525252525252546"},
    {"url": "https://xn--invalid-punycode-0.com/"},
    {"query": "%FF"},
])
def test_network_view_failure_or_matching_never_executes(boundary_stack, monkeypatch, payload):
    gateway, calls, _service = boundary_stack
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: calls.append("executed"))
    result = gateway.invoke("read_url" if "url" in payload else "web_search", payload, "c",
                            user_confirmed=True)
    assert not result.ok and calls == []


def test_new_network_capability_is_not_missed_by_name_allowlist(boundary_stack, monkeypatch):
    from limbowave.application.services import tool_gateway
    from limbowave.domain.permissions import Capability

    gateway, calls, _service = boundary_stack
    monkeypatch.setitem(tool_gateway._CAPABILITY, "future_network_tool", Capability.NETWORK)
    monkeypatch.setattr(gateway, "_invoke", lambda *args, **kwargs: calls.append("executed"))
    result = gateway.invoke("future_network_tool", {"destination": "FORBIDDEN_TOPIC"}, "c",
                            user_confirmed=True)
    assert not result.ok and calls == []


def test_network_scope_is_reset_even_when_invocation_raises(boundary_stack, monkeypatch):
    from limbowave.application.services.network_privacy import check_network_destination

    gateway, _calls, _service = boundary_stack

    def broken(*args, **kwargs):
        raise RuntimeError("execution failed")

    monkeypatch.setattr(gateway, "_invoke", broken)
    with pytest.raises(RuntimeError, match="execution failed"):
        gateway.invoke("read_url", {"url": "https://example.com"}, "c")
    check_network_destination("https://example.com/FORBIDDEN_TOPIC")
