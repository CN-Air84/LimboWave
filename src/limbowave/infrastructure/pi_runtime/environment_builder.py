"""Pi 运行环境构建：把路由决策与密钥变成隔离的 Pi 子进程环境。

核心约束（Phase 1A 验收 + ADR 裁决 3 + GATE-05）：
- **models.json 是运行时派生产物**，不是权威数据源。权威在应用的配置存储。
- **密钥不落 Pi 的 auth.json**：经环境变量注入，models.json 里只写 ``$ENV`` 引用。
- **隔离配置目录**：通过给子进程设置独立的 ``USERPROFILE``/``HOME``，让 Pi 读写
  我们自己的运行时 home，不触碰真实 ``~/.pi``。
- **硬化基线**：关遥测、关项目信任、关内部重试。

密钥处理红线：本模块**绝不**把密钥写进日志、异常文本或任何文件；密钥只进入子进程环境变量。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from limbowave.domain.param_rules import rules_active
from limbowave.domain.providers import ProviderProtocol
from limbowave.domain.routing import RoutingDecision, RoutingError

# 站点参数规则的传递通道（不含密钥，普通环境变量即可）
PARAM_RULES_ENV = "LIMBOWAVE_PARAM_RULES"

# 模型可用的工具清单：全部是应用侧实现（经工具 IPC 通道执行）。
# 与扩展 registerAppTools() 及 tool_gateway._SCHEMAS 一一对应（有测试守）。
APP_TOOLS = (
    "get_current_datetime",
    "read_document",
    "list_directory",
    "stat_file",
    "search_text",
    "create_file",
    "modify_file",
    "read_url",
    "web_search",
    "run_command",
    "add_session_memory",
)


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """一个构建好的 Pi 运行环境。"""

    provider_key: str
    model_id: str
    env: dict[str, str]
    runtime_home: Path


@dataclass(frozen=True, slots=True)
class CatalogSync:
    """一次模型目录热更新的产物。

    - ``env``：Pi 进程里要同步的变量（密钥引用 + 参数规则）。值为空串表示删除。
      **含密钥**——只能经内核送进 Pi 进程，不得记录。
    - ``registered``：登记进 models.json 的 (provider, model)，用于核对热更新结果。
    - ``fingerprint``：目录与环境的摘要，内容未变时可跳过热更新。
    """

    env: dict[str, str]
    registered: tuple[tuple[str, str], ...]
    fingerprint: str


def _secret_env_name(credential_ref: str) -> str:
    """把凭据引用变成合法的环境变量名。"""
    safe = re.sub(r"[^A-Za-z0-9]", "_", credential_ref).upper()
    return f"LIMBOWAVE_SECRET_{safe}"


def _usable(
    entries: Sequence[tuple[RoutingDecision, str | None]],
) -> list[tuple[RoutingDecision, str | None]]:
    """去掉缺密钥的条目：登记进去也只会发未认证请求。"""
    return [
        (decision, secret)
        for decision, secret in entries
        if decision.endpoint.credential_ref is None or secret is not None
    ]


def _catalog_env(entries: Sequence[tuple[RoutingDecision, str | None]]) -> dict[str, str]:
    """目录对应的进程环境：各站点密钥 + 按 provider 分组的参数规则。

    参数规则键总是出现（无规则时为空串）——热更新时要能**清掉**旧规则。
    """
    env: dict[str, str] = {}
    for decision, secret in entries:
        if secret is not None and decision.endpoint.credential_ref is not None:
            env[_secret_env_name(decision.endpoint.credential_ref)] = secret

    # 站点参数规则（§二.4）交给 Pi 侧的扩展执行：请求体在
    # before_provider_request 钩子里被改写（GATE-02 验证过可改写）。
    # 规则**不含密钥**，用普通环境变量传即可。按 provider 键分组——
    # 会话内可能切站点，扩展按当前模型的 provider 取对应规则。
    rules: dict[str, dict[str, list[str]]] = {}
    for decision, _ in entries:
        endpoint = decision.endpoint
        if decision.provider_key in rules or not rules_active(
            whitelist=endpoint.param_whitelist, strip=endpoint.strip_params
        ):
            continue
        rules[decision.provider_key] = {
            "whitelist": list(endpoint.param_whitelist),
            "strip": list(endpoint.strip_params),
        }
    env[PARAM_RULES_ENV] = json.dumps(rules, ensure_ascii=False) if rules else ""
    return env


class EnvironmentBuilder:
    """把路由决策物化为隔离的 Pi 运行环境。"""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def build(
        self,
        decision: RoutingDecision,
        secret: str | None,
        *,
        extra_env: dict[str, str] | None = None,
        catalog: Sequence[tuple[RoutingDecision, str | None]] = (),
    ) -> RuntimeEnvironment:
        """构建运行环境。``secret`` 是已解密的密钥（由 CredentialService 提供）。

        ``catalog`` 是会话内可能切换到的其他 (决策, 密钥)——一并登记进 models.json，
        否则 Pi 的 ``set_model`` 找不到它们（``Model not found``）。缺密钥的条目
        **跳过而不失败**：启动的是主决策，备选站点缺凭据不该拖垮整个内核。
        """
        provider_key = decision.provider_key
        model_id = decision.model_id

        # 需要密钥但缺失：明确失败，不静默发未认证请求。错误不含密钥本体。
        if decision.endpoint.credential_ref is not None and secret is None:
            raise RoutingError(
                f"端点 {decision.endpoint.id} 引用的凭据 {decision.endpoint.credential_ref} "
                "在密钥库中不存在"
            )

        # 主决策排第一：同一 (站点, 模型) 重复时以它的元数据为准
        entries = _usable([(decision, secret), *catalog])

        runtime_home = self._prepare_home()
        self._write_models_json(runtime_home, entries)
        self._write_settings_json(runtime_home)

        env = dict(os.environ)
        # 应用侧注入的环境（如工具 IPC 通道凭据）——由调用方提供，不做默认
        if extra_env:
            env.update(extra_env)
        # 隔离 Pi 的配置目录：Node 的 os.homedir() 在 Windows 优先 USERPROFILE
        env["USERPROFILE"] = str(runtime_home)
        env["HOME"] = str(runtime_home)
        env["PI_SKIP_VERSION_CHECK"] = "1"
        env["PI_OFFLINE"] = "1"
        env.update({name: value for name, value in _catalog_env(entries).items() if value})

        return RuntimeEnvironment(
            provider_key=provider_key,
            model_id=model_id,
            env=env,
            runtime_home=runtime_home,
        )

    def refresh_catalog(
        self, catalog: Sequence[tuple[RoutingDecision, str | None]], *,
        temporary_thinking_levels: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> CatalogSync:
        """热更新：就地重写**正在运行**的 Pi 的 models.json，并给出要同步进进程的环境。

        不重建 runtime home（Pi 正在用它），只覆盖 models.json。
        返回的 ``env`` 必须经内核送进 Pi 进程——新站点的密钥引用要能解析到值。
        """
        entries = _usable(catalog)
        runtime_home = self._runtime_root / "pi-home"
        (runtime_home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
        body = self._write_models_json(runtime_home, entries, temporary_thinking_levels)
        env = _catalog_env(entries)
        registered = tuple(dict.fromkeys((d.provider_key, d.model_id) for d, _ in entries))
        digest = hashlib.sha256(
            (body + json.dumps(env, sort_keys=True)).encode("utf-8")
        ).hexdigest()
        return CatalogSync(env=env, registered=registered, fingerprint=digest)

    # ---------- 内部 ----------

    def _prepare_home(self) -> Path:
        """重建隔离 runtime home（清空旧内容，避免派生产物累积）。"""
        home = self._runtime_root / "pi-home"
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        (home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
        return home

    def _write_models_json(
        self, runtime_home: Path, entries: Sequence[tuple[RoutingDecision, str | None]],
        temporary_thinking_levels: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> str:
        providers: dict[str, dict[str, object]] = {}
        for decision, secret in entries:
            provider = providers.get(decision.provider_key)
            if provider is None:
                provider = self._provider_entry(decision, secret is not None)
                providers[decision.provider_key] = provider
            models = provider["models"]
            assert isinstance(models, list)
            if all(existing["id"] != decision.model_id for existing in models):
                entry = self._model_entry(decision)
                self._allow_thinking_levels(
                    entry, (temporary_thinking_levels or {}).get(
                        (decision.provider_key, decision.model_id), ()
                    )
                )
                models.append(entry)

        payload = {"providers": providers}
        target = runtime_home / ".pi" / "agent" / "models.json"
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        # 内容没变就跳过写盘：热同步可能每次发送前都调一次，反复落盘既无谓
        # 又可能与 Pi 读 models.json 抢。新建的 runtime home（build 路径）
        # 文件不存在，自然写一次。
        if not target.exists() or target.read_text(encoding="utf-8") != body:
            target.write_text(body, encoding="utf-8")
        return body

    @staticmethod
    def _provider_entry(decision: RoutingDecision, has_secret: bool) -> dict[str, object]:
        endpoint = decision.endpoint
        provider: dict[str, object] = {
            "baseUrl": endpoint.base_url,
            "api": endpoint.api.value,
        }
        # 密钥只以 $ENV 引用出现，绝不写明文
        if endpoint.credential_ref is not None and has_secret:
            provider["apiKey"] = f"${_secret_env_name(endpoint.credential_ref)}"
            provider["authHeader"] = True
        if endpoint.headers:
            provider["headers"] = dict(endpoint.headers)
        if endpoint.compat:
            provider["compat"] = dict(endpoint.compat)
        provider["models"] = []
        return provider

    @staticmethod
    def _model_entry(decision: RoutingDecision) -> dict[str, object]:
        model = decision.model
        model_entry: dict[str, object] = {
            "id": decision.model_id,
            "name": model.name,
        }
        if model.context_window is not None:
            model_entry["contextWindow"] = model.context_window
        if model.max_tokens is not None:
            model_entry["maxTokens"] = model.max_tokens
        if model.supports_images:
            model_entry["input"] = ["text", "image"]
        if decision.binding.supports_thinking is not None:
            model_entry["reasoning"] = decision.binding.supports_thinking
        # OpenAI 逐档探测的 effort 必须传给 Pi，否则 xhigh/max 会被钳制到 high。
        # Gemini/Anthropic 的预算探测不等同于逐档 effort 映射。
        levels = decision.binding.available_thinking_levels
        if levels and decision.binding.supports_thinking is True and decision.endpoint.api in (
            ProviderProtocol.OPENAI_COMPLETIONS, ProviderProtocol.OPENAI_RESPONSES
        ):
            model_entry["thinkingLevelMap"] = {
                level: level if level in levels else None
                for level in ("minimal", "low", "medium", "high", "xhigh", "max")
            }
        EnvironmentBuilder._allow_thinking_levels(
            model_entry, decision.binding.user_thinking_levels
        )
        return model_entry

    @staticmethod
    def _allow_thinking_levels(entry: dict[str, object], levels: tuple[str, ...]) -> None:
        if not levels:
            return
        if any(level != "off" for level in levels):
            entry["reasoning"] = True
        current = entry.get("thinkingLevelMap")
        mapping = dict(current) if isinstance(current, dict) else {}
        for level in levels:
            # off 是协议关闭语义，不把它序列化成 effort="off"。
            mapping[level] = "none" if level == "off" else level
        entry["thinkingLevelMap"] = mapping

    def _write_settings_json(self, runtime_home: Path) -> None:
        """硬化基线（ADR 第七节 / GATE-05）+ 内置工具模式（§九.1）。

        关：遥测、项目信任、内核内部重试（重试由应用按站点预设自己管，§八.3）。

        ``defaultTools`` 设为**应用侧工具**：这样模型的文件/联网/命令操作全部
        经应用网关（参数校验 → 权限 → 审计 → 输出限制）。Pi 自带的
        ``read``/``write``/``edit``/``bash`` **有意排除**——它们会绕过应用侧的
        路径守卫与审计，与 §九.1 的「内置工具模式」相抵触。
        """
        settings = {
            "enableInstallTelemetry": False,
            "defaultProjectTrust": "never",
            "retry": {"enabled": False, "provider": {"maxRetries": 0}},
            "defaultTools": list(APP_TOOLS),
        }
        target = runtime_home / ".pi" / "agent" / "settings.json"
        target.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
