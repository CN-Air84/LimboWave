"""Qt-free adapters that expose only public logical models to the LAN facade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from limbowave.application.background import run_blocking
from limbowave.application.kernel import AgentKernel, KernelSetup
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.settings_service import SettingsService
from limbowave.composition import kernel_setup, resolve_catalog
from limbowave.domain.routing import RoutingError
from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder


class RuntimeModelCatalog:
    """Reuse the existing kernel; callers reserve the shared command scope first."""

    def __init__(
        self,
        settings: SettingsService,
        credentials: CredentialService | None,
        runtime_root: Path,
        kernel: Callable[[], AgentKernel | None],
        on_selected: Callable[[KernelSetup], None],
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._runtime_root = runtime_root
        self._kernel = kernel
        self._on_selected = on_selected
        self._fingerprint: str | None = None

    def models(self) -> list[dict[str, Any]]:
        routing = RoutingService(self._settings.load())
        result: list[dict[str, Any]] = []
        for model in routing.list_models():
            try:
                routing.route(model.id)
            except RoutingError:
                continue
            result.append(
                {"id": model.id, "name": model.name, "supports_images": model.supports_images}
            )
        return result

    async def select_model(self, model_id: str) -> None:
        kernel = self._kernel()
        if kernel is None or self._credentials is None:
            raise RuntimeError("runtime_unavailable")
        credentials = self._credentials

        def prepare() -> Any:
            routing = RoutingService(self._settings.load())
            decision = routing.route(model_id)
            sync = EnvironmentBuilder(self._runtime_root).refresh_catalog(
                resolve_catalog(routing, credentials)
            )
            return decision, sync

        decision, sync = await run_blocking(prepare)
        if sync.fingerprint != self._fingerprint:
            await kernel.reload_models(sync.env, sync.registered)
            self._fingerprint = sync.fingerprint
        await kernel.set_model(decision.endpoint.id, decision.model_id)
        selected = kernel_setup(kernel, decision)
        # Commit routing provenance as soon as the physical model changed, even if
        # the optional thinking-level update fails afterwards.
        self._on_selected(selected)
        if selected.default_thinking_level is not None:
            await kernel.set_thinking_level(selected.default_thinking_level)
