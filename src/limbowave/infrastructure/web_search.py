"""Explicit paid-provider adapters; no HTML scraping, native grounding, or fallback."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from limbowave.application.services.web_content import clean_text
from limbowave.domain.network import NetworkSettings, SearchProvider, WebAccessError, WebResponse
from limbowave.infrastructure.web_access import validate_public_url


class SearchTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        method: str = ...,
        headers: Mapping[str, str] | None = ...,
        body: bytes | None = ...,
        max_bytes: int = ...,
        accepted_types: frozenset[str] = ...,
        allow_redirects: bool = ...,
    ) -> WebResponse: ...


def _append_query(url: str, values: Mapping[str, str | int]) -> str:
    parts = urlsplit(url)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *values.items()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class WebSearchClient:
    def __init__(
        self,
        transport: SearchTransport,
        settings: Callable[[], NetworkSettings],
        resolve_secret: Callable[[str | None], str | None],
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._resolve_secret = resolve_secret

    def search(self, query: str) -> dict[str, Any]:
        if not query.strip() or len(query) > 2000 or any(ord(c) < 32 for c in query):
            raise WebAccessError("invalid_query", "搜索词需为 1–2000 字符的非空单行文本")
        try:
            settings = self._settings()
        except Exception:
            raise WebAccessError("configuration_error", "搜索配置无法读取，未发送请求") from None
        provider = settings.search_provider
        if provider is SearchProvider.DISABLED:
            raise WebAccessError(
                "search_not_configured", "网页搜索未配置：请在设置 → 联网搜索选择服务并保存凭据"
            )
        try:
            secret = self._resolve_secret(settings.credential_ref)
        except Exception:
            raise WebAccessError(
                "credentials_unavailable", "搜索凭据库不可读取，请先解锁资料库"
            ) from None
        if not secret:
            raise WebAccessError(
                "credentials_unavailable", "搜索凭据未配置或资料库未解锁；未发送请求"
            )
        if any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise WebAccessError("invalid_credentials", "搜索凭据格式无效，未发送请求")
        try:
            kwargs: dict[str, Any] = {
                "max_bytes": 1024 * 1024,
                "accepted_types": frozenset({"application/json"}),
                "allow_redirects": False,
            }
            result_kind: str
            if provider in {SearchProvider.SERPAPI_GOOGLE, SearchProvider.SERPAPI_BING}:
                engine = "google" if provider is SearchProvider.SERPAPI_GOOGLE else "bing"
                params: dict[str, str | int] = {
                    "engine": engine,
                    "q": query,
                    "api_key": secret,
                    "output": "json",
                }
                if engine == "bing":
                    params["count"] = settings.max_results
                url = "https://serpapi.com/search.json?" + urlencode(params)
                result_kind = "serpapi"
                response = self._transport.request(url, **kwargs)
            elif provider is SearchProvider.TAVILY:
                response = self._transport.request(
                    "https://api.tavily.com/search",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Content-Type": "application/json",
                    },
                    body=json.dumps(
                        {
                            "query": query,
                            "max_results": settings.max_results,
                            "search_depth": "basic",
                            "include_answer": False,
                            "include_raw_content": False,
                            "include_images": False,
                        }
                    ).encode("utf-8"),
                    **kwargs,
                )
                result_kind = "tavily"
            elif provider is SearchProvider.AZURE_BING:
                url = "https://api.bing.microsoft.com/v7.0/search?" + urlencode(
                    {
                        "q": query,
                        "count": settings.max_results,
                        "responseFilter": "Webpages",
                        "textDecorations": "false",
                        "textFormat": "Raw",
                    }
                )
                response = self._transport.request(
                    url, headers={"Ocp-Apim-Subscription-Key": secret}, **kwargs
                )
                result_kind = "azure_bing"
            else:
                endpoint = settings.custom_search_endpoint
                if not endpoint:
                    raise WebAccessError("configuration_error", "自定义搜索 API 地址未配置")
                endpoint, _, _ = validate_public_url(endpoint)
                url = _append_query(endpoint, {"q": query, "count": settings.max_results})
                if len(url) > 4096:
                    raise WebAccessError("invalid_query", "搜索词编码后的请求过长，请缩短关键词")
                response = self._transport.request(
                    url, headers={"Authorization": f"Bearer {secret}"}, **kwargs
                )
                result_kind = "custom"
            if response.truncated:
                raise WebAccessError("response_too_large", "搜索响应超过大小上限，未返回不完整结果")
            if not 200 <= response.status < 300:
                raise WebAccessError(
                    "http_status", "搜索服务请求失败", data={"status": response.status}
                )
            payload = json.loads(response.body)
            if not isinstance(payload, dict):
                raise WebAccessError("invalid_response", "搜索服务返回了无效数据")
            if payload.get("error") or payload.get("errors"):
                raise WebAccessError(
                    "provider_error", "搜索服务报告失败，请检查凭据、额度与服务状态"
                )
            if result_kind == "serpapi":
                rows = payload.get("organic_results")
                info = payload.get("search_information")
                if (
                    rows is None
                    and isinstance(info, dict)
                    and (
                        info.get("total_results") == 0
                        or info.get("organic_results_state") == "Fully empty"
                    )
                ):
                    rows = []
            elif result_kind == "azure_bing":
                pages = payload.get("webPages")
                rows = pages.get("value") if isinstance(pages, dict) else None
                if (
                    rows is None
                    and isinstance(pages, dict)
                    and pages.get("totalEstimatedMatches") == 0
                ):
                    rows = []
            else:
                rows = payload.get("results")
            if not isinstance(rows, list):
                raise WebAccessError("invalid_response", "搜索服务未返回可核验的结果列表")
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            omitted = 0
            for row in rows:
                if not isinstance(row, dict):
                    omitted += 1
                    continue
                target = row.get("link" if result_kind == "serpapi" else "url")
                if not isinstance(target, str) or secret in target:
                    omitted += 1
                    continue
                try:
                    target, _, _ = validate_public_url(target)
                except WebAccessError:
                    omitted += 1
                    continue
                if target in seen:
                    omitted += 1
                    continue
                seen.add(target)
                if len(results) >= settings.max_results:
                    omitted += 1
                    continue

                def text(value: Any, limit: int) -> str:
                    return (
                        clean_text(value.replace(secret, "[redacted]"))[:limit]
                        if isinstance(value, str)
                        else ""
                    )

                snippet_field = "content" if result_kind == "tavily" else "snippet"
                result: dict[str, Any] = {
                    "title": text(row.get("title") or row.get("name"), 300),
                    "url": target,
                    "snippet": text(row.get(snippet_field), 1200),
                    "source_id": len(results) + 1,
                }
                date = text(
                    row.get("date") or row.get("published_date") or row.get("datePublished"),
                    100,
                )
                if date:
                    result["published_date"] = date
                results.append(result)
            engine, label = {
                SearchProvider.SERPAPI_GOOGLE: ("google", "Google（经 SerpAPI，非官方直连）"),
                SearchProvider.SERPAPI_BING: ("bing", "Bing（经 SerpAPI，非官方直连）"),
                SearchProvider.TAVILY: ("tavily", "Tavily"),
                SearchProvider.AZURE_BING: ("bing", "Microsoft Azure Bing Web Search（旧版兼容）"),
                SearchProvider.CUSTOM_JSON: ("custom", "自定义 JSON 搜索 API"),
            }[provider]
            return {
                "query": query,
                "provider": provider.value,
                "engine": engine,
                "provider_label": label,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "results": results,
                "count": len(results),
                "omitted_results": omitted,
                "truncated": omitted > 0,
                "evidence_type": "search_snippets",
                "trust": "untrusted_external_content",
            }
        except WebAccessError as exc:
            if exc.code == "http_status":
                status = exc.data.get("status")
                if status in {401, 403}:
                    raise WebAccessError(
                        "authentication_failed", "搜索服务拒绝凭据，请检查 API Key 和账户权限"
                    ) from None
                if status in {402, 429, 432, 433}:
                    raise WebAccessError(
                        "quota_or_rate_limit",
                        "搜索服务额度不足或请求过于频繁；未自动重试或切换服务",
                    ) from None
            raise
        except Exception:
            raise WebAccessError(
                "search_failed", "搜索请求失败或响应无效，未取得可核验结果"
            ) from None
