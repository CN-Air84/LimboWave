"""Headless server composition without importing the Qt desktop entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from limbowave.application.kernel import AgentKernel, KernelSetup
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.event_broker import EventBroker
from limbowave.application.services.memory_service import MemoryService
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.run_coordinator import RunContext
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.application.services.settings_service import SettingsService
from limbowave.bootstrap import AppPaths
from limbowave.composition import build_kernel
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.change_log import ChangeLogStore
from limbowave.infrastructure.database.sqlite_repositories import SqliteUnitOfWorkFactory
from limbowave.runtime_composition import RuntimeModelCatalog

from .command_store import PersistentCommandStore
from .tool_policy import ServerToolPolicy


@dataclass(slots=True)
class ServerRuntime:
    """All unlocked server resources and their shutdown order."""

    paths: AppPaths
    key: VaultKey
    storage: SqliteUnitOfWorkFactory
    controller: SessionController
    facade: RuntimeFacade
    model_catalog: RuntimeModelCatalog | None = None
    tool_policy: ServerToolPolicy = field(default_factory=ServerToolPolicy)

    async def start(self) -> None:
        await self.controller.start()

    async def close(self) -> None:
        # Stop generation before releasing the encrypted storage connection.
        try:
            await self.controller.shutdown()
        finally:
            await self.facade.close()
            self.storage.close()


def _setup_context(state: dict[str, KernelSetup | None]) -> Callable[[], RunContext]:
    def context() -> RunContext:
        active = state["value"]
        if active is None:
            return RunContext("unknown", "unknown", "未配置可用模型")
        return RunContext(
            active.logical_model_id,
            active.endpoint_id,
            active.routing_reason,
            app_params=dict(active.app_params),
            supports_images=active.supports_images,
            retry_policy=active.retry_policy,
            thinking_level=active.default_thinking_level,
        )

    return context


def _models(paths: AppPaths) -> list[dict[str, Any]]:
    settings = SettingsService(
        ConfigurationService(JsonConfigRepository(paths.data_root / "config.json"))
    )
    result: list[dict[str, Any]] = []
    routing = RoutingService(settings.load())
    for model in routing.list_models():
        result.append({
            "id": model.id, "name": model.name, "supports_images": model.supports_images
        })
    return result


def build_server_runtime(
    paths: AppPaths,
    key: VaultKey,
    *,
    kernel: AgentKernel | None = None,
) -> ServerRuntime:
    """Build the same application services as desktop, without Qt-only wiring.

    ``kernel`` is injectable for Linux smoke tests. Production callers leave it as
    ``None`` and reuse the existing composition root to construct Pi.
    """
    paths.data_root.mkdir(parents=True, exist_ok=True)
    storage = SqliteUnitOfWorkFactory(paths.data_root / "limbowave.db", key)
    setup = (
        None
        if kernel is not None
        else build_kernel(paths, key, extra_env={"LIMBOWAVE_SERVER": "1"})
    )
    active_kernel = kernel or (setup.kernel if setup is not None else None)
    setup_state: dict[str, KernelSetup | None] = {"value": setup}
    context = _setup_context(setup_state)
    controller = SessionController(active_kernel, uow_factory=storage, context=context)
    memories = MemoryService(
        storage,
        ConfigurationService(JsonConfigRepository(paths.data_root / "config.json")),
    )
    controller.set_memory_service(memories)

    settings = SettingsService(
        ConfigurationService(JsonConfigRepository(paths.data_root / "config.json"))
    )
    credentials = CredentialService(SecretStore(key, paths.data_root / "vault" / "secrets.json"))
    catalog: RuntimeModelCatalog | None = None
    def on_selected(selected: KernelSetup) -> None:
        setup_state["value"] = selected

    if active_kernel is not None:
        catalog = RuntimeModelCatalog(
            settings,
            credentials,
            paths.data_root / "runtime",
            lambda: controller.coordinator().kernel,
            on_selected,
        )

    def model_provider() -> list[dict[str, Any]]:
        return catalog.models() if catalog is not None else _models(paths)

    broker = EventBroker()
    facade = RuntimeFacade(
        controller,
        storage,
        models_provider=model_provider,
        select_model=catalog.select_model if catalog is not None else None,
        broker=broker,
        receipts=PersistentCommandStore(paths.data_root / "limbowave.db", broker.epoch),
        memory_service=memories,
        sync_store=ChangeLogStore(
            paths.data_root / "limbowave.db", uow_factory=storage, key=key
        ),
    )
    return ServerRuntime(paths, key, storage, controller, facade, catalog)
