from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
from limbowave.domain.network import WebAccessError, WebResponse
from limbowave.domain.permissions import Capability
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


@pytest.fixture
def setup(tmp_path):
    permissions = PermissionService(in_memory_uow_factory(InMemoryStore()))
    calls = []

    def search(query):
        calls.append(query)
        return {
            "results": [{"url": "https://example.com/a", "title": "News", "snippet": "fact"}],
            "count": 1,
            "evidence_type": "search_snippets",
            "retrieved_at": "2026-10-27T00:00:00Z",
        }

    def fetch(url):
        calls.append(url)
        return WebResponse(
            url,
            200,
            "text/html",
            b"<h1>Article</h1><p>Verified content</p>",
            "2026-10-27T00:00:00Z",
        )

    gateway = ToolGateway(
        permissions, tmp_path, network=NetworkTools(page_fetcher=fetch, searcher=search)
    )
    return gateway, permissions, calls


def test_search_requires_confirmation_even_with_wildcard_domain_grant(setup):
    gateway, permissions, calls = setup
    permissions.grant("c", Capability.NETWORK, allowed_domains=("*",))
    assert not gateway.invoke("web_search", {"query": "news"}, "c").ok
    assert calls == []
    result = gateway.invoke("web_search", {"query": "news"}, "c", user_confirmed=True)
    assert result.ok and result.data["count"] == 1
    assert any("摘要" in note for note in result.notes)
    assert len(calls) == 1


def test_page_denial_prevents_http_and_dns(setup):
    gateway, _, calls = setup
    assert not gateway.invoke("read_url", {"url": "https://example.com/"}, "c").ok
    assert calls == []


@pytest.mark.parametrize(
    "tool,params",
    [("web_search", {"query": "news"}), ("read_url", {"url": "https://example.com/"})],
)
def test_remote_origin_cannot_bypass_with_confirmation_or_grant(setup, tool, params):
    gateway, permissions, calls = setup
    permissions.grant("c", Capability.NETWORK, allowed_domains=("*",))
    gateway.origin_provider = lambda: "web"
    assert not gateway.invoke(tool, params, "c", user_confirmed=True).ok
    assert calls == []


def test_page_evidence_contains_provenance_and_warning(setup):
    gateway, permissions, _ = setup
    permissions.grant("c", Capability.NETWORK, allowed_domains=("example.com",))
    result = gateway.invoke("read_url", {"url": "https://example.com/article"}, "c")
    assert result.ok and result.data["text"] == "Article\nVerified content"
    assert result.data["evidence_type"] == "page_content"
    assert result.data["retrieved_at"] == "2026-10-27T00:00:00Z"
    assert any("不是指令" in note for note in result.notes)


def test_safe_error_code_reaches_model(setup):
    gateway, _, _ = setup

    def fail(query):
        raise WebAccessError("quota_or_rate_limit", "额度不足")

    gateway._network = NetworkTools(searcher=fail)
    result = gateway.invoke("web_search", {"query": "news"}, "c", user_confirmed=True)
    assert not result.ok and result.data["code"] == "quota_or_rate_limit"


def test_full_search_payload_is_limited_not_just_text(setup):
    gateway, _, _ = setup
    gateway._max_output_bytes = 1000
    gateway._network = NetworkTools(
        searcher=lambda query: {
            "evidence_type": "search_snippets",
            "count": 10,
            "results": [
                {"url": f"https://example.com/{i}", "snippet": "x" * 600} for i in range(10)
            ],
        }
    )
    result = gateway.invoke("web_search", {"query": "news"}, "c", user_confirmed=True)
    assert result.ok and result.truncated
    assert len(json.dumps(result.data, ensure_ascii=False).encode()) <= 1000
    assert result.data["count"] == len(result.data["results"])


@pytest.mark.parametrize(
    "tool,params",
    [
        ("web_search", {"query": " "}),
        ("web_search", {"query": "x" * 2001}),
        ("read_url", {"url": "https://[invalid/"}),
    ],
)
def test_bad_parameters_do_not_execute_network(setup, tool, params):
    gateway, _, calls = setup
    assert not gateway.invoke(tool, params, "c", user_confirmed=True).ok
    assert calls == []


def test_desktop_network_composition_precedes_ipc_start():
    source = Path("src/limbowave/app.py").read_text(encoding="utf-8")
    assert source.index("network=create_network_tools(settings, credentials)") < source.index(
        "loop.run_until_complete(tool_ipc.start())"
    )
    assert source.index("credentials = (", source.index("def _wire")) < source.index(
        "network=create_network_tools(settings, credentials)"
    )
