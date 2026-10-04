"""组合根：把权威配置装配成可用的 AgentKernel。

链路：权威配置 → 路由决策 → 凭据解析 → 隔离运行环境 → PiKernelAdapter。

返回 :class:`KernelSetup` 而非裸内核：**路由决策要随内核一起交出去**，
否则上层无法为「请求意图快照」记录"本来打算发往哪个逻辑模型的哪个端点、为什么"。

任何一步缺失或失败都返回 None（优雅降级），不抛给 GUI。
密钥解析失败（如端点引用了不存在的凭据）也按"无内核"降级，而非崩溃——
因为密钥缺失是配置问题，不是运行时崩溃。资料库未解锁（无 ``VaultKey``）时同样降级：
凭据无法解密，内核无从谈起。

**密钥红线：本模块不把密钥写进日志或异常；密钥只进入子进程环境变量。**
"""

from __future__ import annotations

import logging

from limbowave.application.kernel import AgentKernel, KernelSetup
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.routing_service import RoutingService
from limbowave.bootstrap import AppPaths
from limbowave.domain.routing import RoutingDecision, RoutingError
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultError, VaultKey
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder

_LOG = logging.getLogger(__name__)


def build_kernel(
    paths: AppPaths,
    key: VaultKey | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> KernelSetup | None:
    """从权威配置构建内核。返回 None 表示无可用内核（优雅降级）。

    ``key`` 为 None（资料库未解锁）时无法解析凭据，直接按"无内核"降级。
    """
    if key is None:
        _LOG.info("kernel.unavailable", extra={"reason": "vault_locked"})
        return None

    config_path = paths.data_root / "config.json"
    vault_dir = paths.data_root / "vault"
    runtime_root = paths.data_root / "runtime"

    config = ConfigurationService(JsonConfigRepository(config_path)).load()
    routing = RoutingService(config)
    try:
        decision = routing.route()
    except RoutingError:
        _LOG.warning("kernel.unavailable", extra={"reason": "no_route"})
        return None

    credential = CredentialService(SecretStore(key, vault_dir / "secrets.json"))
    try:
        secret = credential.resolve(decision.endpoint.credential_ref)
    except VaultError:
        # 密文损坏或未经迁移的旧格式：按"无凭据"降级，不抛给 GUI
        _LOG.warning("kernel.unavailable", extra={"reason": "credential_unavailable"})
        return None

    # 会话内可切换的其他逻辑模型/站点：一并登记给 Pi（否则 set_model 找不到）。
    catalog = resolve_catalog(routing, credential)

    try:
        runtime_env = EnvironmentBuilder(runtime_root).build(
            decision, secret, extra_env=extra_env, catalog=catalog
        )
    except RoutingError:
        _LOG.warning("kernel.unavailable", extra={"reason": "runtime_environment"})
        return None

    try:
        spec = build_spawn_spec(
            provider=runtime_env.provider_key,
            model_id=runtime_env.model_id,
            cwd=paths.data_root,
            env=runtime_env.env,
        )
        kernel = PiKernelAdapter(spec)
    except Exception as exc:
        # 不将进程参数、环境或凭据异常的原文写入明文诊断。
        _LOG.warning("kernel.unavailable", extra={
            "reason": "runtime_setup", "error_type": type(exc).__name__,
        })
        return None

    _LOG.info("kernel.configured", extra={
        "logical_model_id": decision.model.id, "endpoint_id": decision.endpoint.id,
    })
    return kernel_setup(kernel, decision)


def kernel_setup(kernel: AgentKernel, decision: RoutingDecision) -> KernelSetup:
    """路由决策 + 内核 → 交给上层的运行上下文。会话内切换模型也用它重建。"""
    return KernelSetup(
        kernel=kernel,
        logical_model_id=decision.model.id,
        endpoint_id=decision.endpoint.id,
        routing_reason=decision.reason,
        app_params=_app_params(decision),
        supports_images=decision.model.supports_images,
        retry_policy=decision.endpoint.retry.to_policy(),
        default_thinking_level=decision.binding.default_thinking_level,
        thinking_level_locked=decision.binding.thinking_level_locked,
        available_thinking_levels=decision.binding.available_thinking_levels,
        supports_thinking=decision.binding.supports_thinking,
        supports_tools=decision.binding.supports_tools,
    )


def resolve_catalog(
    routing: RoutingService, credential: CredentialService
) -> list[tuple[RoutingDecision, str | None]]:
    """全部可切换的 (决策, 密钥)。启动与热更新共用。

    某条凭据解不开只跳过该条（记为无密钥，由环境构建器剔除），不影响其他站点。
    """
    catalog: list[tuple[RoutingDecision, str | None]] = []
    for item in routing.catalog():
        try:
            catalog.append((item, credential.resolve(item.endpoint.credential_ref)))
        except VaultError:
            _LOG.warning("kernel.catalog_credential_unavailable", extra={
                "endpoint_id": item.endpoint.id,
            })
            continue
    return catalog


def _app_params(decision: RoutingDecision) -> dict[str, object]:
    """应用侧实际参与请求的参数（用于与实际上线请求体做键级差异）。

    只列应用能解释来源的字段：模型 ID、输出上限、上下文窗口。Pi 自己补的字段
    （``stream`` / ``stream_options`` / ``store`` 等）不会出现在这里，
    差异因此可被显式观察——这是设计计划 §4.4 的可观测化。
    """
    params: dict[str, object] = {"model": decision.model_id}
    if decision.model.max_tokens is not None:
        params["max_tokens"] = decision.model.max_tokens
    if decision.model.context_window is not None:
        params["context_window"] = decision.model.context_window
    return params
