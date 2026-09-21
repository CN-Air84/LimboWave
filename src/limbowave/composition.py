"""组合根：把权威配置装配成可用的 AgentKernel。

链路：权威配置 → 路由决策 → 凭据解析 → 隔离运行环境 → PiKernelAdapter。

任何一步缺失或失败都返回 None（优雅降级），不抛给 GUI。
密钥解析失败（如端点引用了不存在的凭据）也按"无内核"降级，而非崩溃——
因为密钥缺失是配置问题，不是运行时崩溃。

**密钥红线：本模块不把密钥写进日志或异常；密钥只进入子进程环境变量。**
"""

from __future__ import annotations

from limbowave.application.kernel import AgentKernel
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.routing_service import RoutingService
from limbowave.bootstrap import AppPaths
from limbowave.domain.routing import RoutingError
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder


def build_kernel(paths: AppPaths) -> AgentKernel | None:
    """从权威配置构建内核。返回 None 表示无可用内核（优雅降级）。"""
    config_path = paths.data_root / "config.json"
    vault_dir = paths.data_root / "vault"
    runtime_root = paths.data_root / "runtime"

    config = ConfigurationService(JsonConfigRepository(config_path)).load()
    routing = RoutingService(config)
    try:
        decision = routing.route()
    except RoutingError:
        return None

    credential = CredentialService(SecretStore(vault_dir))
    secret = credential.resolve(decision.endpoint.credential_ref)

    try:
        runtime_env = EnvironmentBuilder(runtime_root).build(decision, secret)
    except RoutingError:
        return None

    try:
        spec = build_spawn_spec(
            provider=runtime_env.provider_key,
            model_id=runtime_env.model_id,
            cwd=paths.data_root,
            env=runtime_env.env,
        )
        return PiKernelAdapter(spec)
    except Exception:
        # node / Pi 不可用等
        return None
