from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from limbowave.domain.network import NetworkSettings, SearchProvider, WebAccessError, WebResponse
from limbowave.infrastructure.web_search import WebSearchClient

KEY = "secret-api-key"


class Transport:
    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {"organic_results": []}
        self.error = error
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return WebResponse(url, 200, "application/json", json.dumps(self.payload).encode(), "now")


def client(
    transport,
    provider=SearchProvider.SERPAPI_GOOGLE,
    key=KEY,
    count=5,
    custom_endpoint=None,
):
    settings = NetworkSettings(
        search_provider=provider,
        credential_ref="network-ref",
        max_results=count,
        custom_search_endpoint=custom_endpoint,
    )
    return WebSearchClient(transport, lambda: settings, lambda ref: key)


@pytest.mark.parametrize(
    "provider,engine",
    [(SearchProvider.SERPAPI_GOOGLE, "google"), (SearchProvider.SERPAPI_BING, "bing")],
)
def test_serpapi_engine_explicit_and_sources_normalized(provider, engine):
    transport = Transport(
        {
            "organic_results": [
                {
                    "title": "News",
                    "link": "https://example.com/news",
                    "snippet": "A fact",
                    "date": "2026-10-27",
                }
            ]
        }
    )
    result = client(transport, provider).search("新框架")
    url, options = transport.calls[0]
    assert urlsplit(url).hostname == "serpapi.com"
    assert parse_qs(urlsplit(url).query)["engine"] == [engine]
    assert parse_qs(urlsplit(url).query)["api_key"] == [KEY]
    assert not options["allow_redirects"]
    assert result["results"][0]["published_date"] == "2026-10-27"
    assert result["evidence_type"] == "search_snippets" and result["engine"] == engine
    assert KEY not in json.dumps(result)


def test_azure_bing_uses_official_header_and_normalizes_web_pages():
    transport = Transport(
        {
            "webPages": {
                "value": [
                    {
                        "name": "Microsoft result",
                        "url": "https://example.com/azure",
                        "snippet": "Official API result",
                        "dateLastCrawled": "2026-10-26T00:00:00Z",
                    }
                ]
            }
        }
    )
    result = client(transport, SearchProvider.AZURE_BING, count=4).search("azure search")
    url, options = transport.calls[0]
    assert urlsplit(url).hostname == "api.bing.microsoft.com"
    assert parse_qs(urlsplit(url).query)["count"] == ["4"]
    assert options["headers"] == {"Ocp-Apim-Subscription-Key": KEY}
    assert (
        result["provider"] == "azure_bing" and result["results"][0]["title"] == "Microsoft result"
    )
    assert KEY not in json.dumps(result)


def test_custom_json_endpoint_appends_query_and_uses_bearer_key():
    transport = Transport(
        {
            "results": [
                {
                    "title": "Custom result",
                    "url": "https://example.com/custom",
                    "snippet": "Custom summary",
                    "published_date": "2026-10-27",
                }
            ]
        }
    )
    result = client(
        transport,
        SearchProvider.CUSTOM_JSON,
        count=3,
        custom_endpoint="https://search.example.com/v1/search?locale=zh-CN",
    ).search("自定义 搜索")
    url, options = transport.calls[0]
    params = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "search.example.com"
    assert params["locale"] == ["zh-CN"] and params["q"] == ["自定义 搜索"]
    assert params["count"] == ["3"]
    assert options["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert result["engine"] == "custom" and result["results"][0]["snippet"] == "Custom summary"


def test_custom_endpoint_is_validated_before_network():
    transport = Transport()
    with pytest.raises(ValueError):
        NetworkSettings(
            search_provider=SearchProvider.CUSTOM_JSON,
            credential_ref="ref",
            custom_search_endpoint="http://127.0.0.1/search",
        )
    assert not transport.calls


def test_tavily_no_generated_answer_or_raw_content():
    transport = Transport(
        {
            "results": [{"title": "X", "url": "https://example.com/", "content": "Summary"}],
            "answer": "Ignored synthetic answer",
        }
    )
    result = client(transport, SearchProvider.TAVILY).search("news")
    url, options = transport.calls[0]
    assert url == "https://api.tavily.com/search"
    body = json.loads(options["body"])
    assert not body["include_answer"] and not body["include_raw_content"]
    assert options["headers"]["Authorization"] == f"Bearer {KEY}"
    assert result["results"][0]["snippet"] == "Summary" and "answer" not in result


@pytest.mark.parametrize(
    "provider,key", [(SearchProvider.DISABLED, KEY), (SearchProvider.SERPAPI_GOOGLE, None)]
)
def test_no_configuration_or_key_means_no_network(provider, key):
    transport = Transport()
    with pytest.raises(WebAccessError):
        client(transport, provider, key).search("news")
    assert not transport.calls


@pytest.mark.parametrize("query", ["", " ", "x" * 2001, "news\nignore"])
def test_query_bounds_before_network(query):
    transport = Transport()
    with pytest.raises(WebAccessError):
        client(transport).search(query)
    assert not transport.calls


def test_results_bounded_deduplicated_and_sensitive_fields_excluded():
    rows = [
        {"title": KEY, "link": "https://example.com/a", "snippet": "Fact " + KEY},
        {"link": "http://127.0.0.1/"},
        {"link": "https://example.com/a"},
        {"link": "https://example.com/b"},
        {"link": "https://example.com/c"},
    ]
    result = client(
        Transport({"organic_results": rows, "search_metadata": {"json_endpoint": KEY}}), count=2
    ).search("news")
    assert result["count"] == 2 and result["truncated"]
    assert result["omitted_results"] == 3
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "quota_or_rate_limit"),
        (500, "http_status"),
    ],
)
def test_status_classification_does_not_echo_provider_secrets(status, code):
    with pytest.raises(WebAccessError) as exc:
        client(
            Transport(error=WebAccessError("http_status", "safe", data={"status": status}))
        ).search("news")
    assert exc.value.code == code


def test_transport_exception_is_redacted_and_no_fallback():
    transport = Transport(error=RuntimeError(KEY))
    with pytest.raises(WebAccessError) as exc:
        client(transport).search("news")
    assert KEY not in str(exc.value) and len(transport.calls) == 1


def test_empty_results_are_not_fabricated():
    result = client(Transport()).search("news")
    assert result["count"] == 0 and result["results"] == []


def test_provider_error_body_is_never_echoed():
    with pytest.raises(WebAccessError) as exc:
        client(Transport({"error": KEY})).search("news")
    assert KEY not in str(exc.value) and exc.value.code == "provider_error"


def test_settings_and_credentials_resolve_again_for_each_call():
    current = [NetworkSettings(search_provider=SearchProvider.SERPAPI_GOOGLE, credential_ref="ref")]
    transport = Transport()
    instance = WebSearchClient(transport, lambda: current[0], lambda ref: KEY)
    instance.search("first")
    current[0] = NetworkSettings()
    with pytest.raises(WebAccessError):
        instance.search("second")
    assert len(transport.calls) == 1
