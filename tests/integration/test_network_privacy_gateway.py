"""Real gateway -> invocation scope -> pinned HTTP adapter, without network I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.network import NetworkSettings, SearchProvider
from limbowave.domain.permissions import PermissionPreset
from limbowave.infrastructure.web_access import PublicHTTPClient
from limbowave.infrastructure.web_search import WebSearchClient
from tests.unit.test_memory_service import memory_stack as memory_stack
from tests.unit.test_web_access import Connection, Response


@pytest.fixture
def integrated_boundary(memory_stack, tmp_path, monkeypatch):
    memory, factory = memory_stack
    with factory() as uow:
        uow.messages.add(
            Message(
                "protected-source",
                "c",
                "b",
                MessageRole.USER,
                "EXACT_PROTECTED_SOURCE",
                datetime.now(UTC),
            )
        )
        uow.commit()
    memory.privacy.add("blockedtopic", source_ids=["protected-source"])
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    dns = []
    requests = []
    responses = []

    def resolve(host):
        dns.append(host)
        return ["93.184.216.34"]

    def connect(scheme, host, port, ips, timeout):
        connection = Connection(responses.pop(0))
        requests.append(connection)
        return connection

    transport = PublicHTTPClient(resolver=resolve)
    monkeypatch.setattr(transport, "_connection", connect)
    gateway = ToolGateway(
        permissions,
        tmp_path,
        network=NetworkTools(page_fetcher=transport.request),
    )
    gateway.memory_service = memory
    return gateway, transport, dns, requests, responses


@pytest.mark.parametrize(
    "target", ["/blockedtopic", "/%62lockedtopic", "https://other.example/blockedtopic"]
)
def test_real_gateway_blocks_redirect_before_second_dns(integrated_boundary, target):
    gateway, transport, dns, requests, responses = integrated_boundary
    responses.append(Response(status=302, headers={"Location": target}))
    result = gateway.invoke(
        "read_url", {"url": "https://example.com/safe"}, "c", user_confirmed=True
    )
    assert not result.ok and result.data.get("code") == "privacy_denied"
    assert dns == ["example.com"]
    assert len(requests) == 1 and requests[0].closed
    assert "blockedtopic" not in str(result) and "%62" not in str(result)
    # The shared transport must not retain the finished invocation's policy.
    responses.append(Response())
    assert transport.request("https://example.com/blockedtopic").body == b"hello"
    assert len(requests) == 2


def test_real_gateway_keeps_safe_redirect_and_provenance(integrated_boundary):
    gateway, _, dns, requests, responses = integrated_boundary
    responses.extend(
        [
            Response(status=302, headers={"Location": "/article"}),
            Response(b"verified public article"),
        ]
    )
    result = gateway.invoke(
        "read_url", {"url": "https://example.com/safe"}, "c", user_confirmed=True
    )
    assert result.ok
    assert dns == ["example.com", "example.com"]
    assert len(requests) == 2 and all(request.closed for request in requests)
    assert result.data["url"] == "https://example.com/article"
    assert result.data["text"] == "verified public article"
    assert result.data["evidence_type"] == "page_content"
    assert result.data["retrieved_at"]


@pytest.mark.parametrize(
    "provider", [SearchProvider.SERPAPI_GOOGLE, SearchProvider.SERPAPI_BING, SearchProvider.TAVILY]
)
def test_real_search_client_never_resolves_key_for_blocked_query(integrated_boundary, provider):
    gateway, transport, dns, requests, _ = integrated_boundary
    resolutions = []

    def secret(ref):
        resolutions.append(ref)
        return "offline-test-key"

    search = WebSearchClient(
        transport,
        lambda: NetworkSettings(
            search_provider=provider,
            credential_ref="offline-test-ref",
        ),
        secret,
    )
    gateway._network = NetworkTools(page_fetcher=transport.request, searcher=search.search)
    result = gateway.invoke("web_search", {"query": "blockedtopic"}, "c", user_confirmed=True)
    assert not result.ok and not result.data
    assert resolutions == [] and dns == [] and requests == []


def test_real_page_extraction_cannot_wrap_protected_source(integrated_boundary):
    gateway, _, _, requests, responses = integrated_boundary
    responses.append(Response(b"EXACT_PROTECTED_SOURCE"))
    result = gateway.invoke(
        "read_url", {"url": "https://example.com/safe"}, "c", user_confirmed=True
    )
    assert len(requests) == 1
    assert not result.ok and result.data == {}
    assert "EXACT_PROTECTED_SOURCE" not in str(result)


async def test_actual_gateway_installs_scope_inside_raw_executor(integrated_boundary):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    from limbowave.application.services.network_privacy import check_network_destination

    gateway, _transport, dns, requests, responses = integrated_boundary
    responses.append(Response(status=302, headers={"Location": "/blockedtopic"}))
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        denied = await loop.run_in_executor(
            executor,
            partial(
                gateway.invoke,
                "read_url",
                {"url": "https://example.com/safe"},
                "c",
                user_confirmed=True,
            ),
        )
        assert not denied.ok and dns == ["example.com"] and len(requests) == 1
        # Neither the caller nor a reused executor worker inherits a stale policy.
        check_network_destination("https://example.com/blockedtopic")
        await loop.run_in_executor(
            executor, check_network_destination, "https://example.com/blockedtopic"
        )
        responses.append(Response(b"public follow-up"))
        allowed = await loop.run_in_executor(
            executor,
            partial(
                gateway.invoke,
                "read_url",
                {"url": "https://example.com/another"},
                "c",
                user_confirmed=True,
            ),
        )
        assert allowed.ok and allowed.data["text"] == "public follow-up"
