"""Desktop composition for real network tools, constructed before tool IPC starts."""

from __future__ import annotations

from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.application.services.tool_gateway import NetworkTools
from limbowave.domain.network import SearchProvider
from limbowave.infrastructure.web_access import PublicHTTPClient
from limbowave.infrastructure.web_search import WebSearchClient


def create_network_tools(
    settings: SettingsService,
    credentials: CredentialService | None,
) -> NetworkTools:
    # PublicHTTPClient consults the invocation's ContextVar URL guards on every
    # hop. Reuse the stateless transport; never capture a conversation's checker
    # here or when settings are loaded (the next call may belong to another run).
    transport = PublicHTTPClient()
    search = WebSearchClient(
        transport,
        lambda: settings.load().network,
        credentials.resolve if credentials is not None else lambda ref: None,
    )

    def search_ready() -> bool:
        # Presence only: do not resolve/export credentials or make a network call.
        config = settings.load().network
        return (
            config.search_provider is not SearchProvider.DISABLED
            and credentials is not None
            and config.credential_ref is not None
            and config.credential_ref in credentials.list_refs()
        )

    return NetworkTools(
        page_fetcher=transport.request,
        searcher=search.search,
        search_ready=search_ready,
    )
