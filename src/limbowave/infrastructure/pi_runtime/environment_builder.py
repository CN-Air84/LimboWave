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

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from limbowave.domain.routing import RoutingDecision, RoutingError


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """一个构建好的 Pi 运行环境。"""

    provider_key: str
    model_id: str
    env: dict[str, str]
    runtime_home: Path


def _secret_env_name(credential_ref: str) -> str:
    """把凭据引用变成合法的环境变量名。"""
    safe = re.sub(r"[^A-Za-z0-9]", "_", credential_ref).upper()
    return f"LIMBOWAVE_SECRET_{safe}"


class EnvironmentBuilder:
    """把路由决策物化为隔离的 Pi 运行环境。"""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def build(self, decision: RoutingDecision, secret: str | None) -> RuntimeEnvironment:
        """构建运行环境。``secret`` 是已解密的密钥（由 CredentialService 提供）。"""
        provider_key = decision.provider_key
        model_id = decision.model_id

        # 需要密钥但缺失：明确失败，不静默发未认证请求。错误不含密钥本体。
        if decision.endpoint.credential_ref is not None and secret is None:
            raise RoutingError(
                f"端点 {decision.endpoint.id} 引用的凭据 {decision.endpoint.credential_ref} "
                "在密钥库中不存在"
            )

        runtime_home = self._prepare_home()
        self._write_models_json(runtime_home, decision, secret is not None)
        self._write_settings_json(runtime_home)

        env = dict(os.environ)
        # 隔离 Pi 的配置目录：Node 的 os.homedir() 在 Windows 优先 USERPROFILE
        env["USERPROFILE"] = str(runtime_home)
        env["HOME"] = str(runtime_home)
        env["PI_SKIP_VERSION_CHECK"] = "1"
        env["PI_OFFLINE"] = "1"

        if secret is not None and decision.endpoint.credential_ref is not None:
            env[_secret_env_name(decision.endpoint.credential_ref)] = secret

        return RuntimeEnvironment(
            provider_key=provider_key,
            model_id=model_id,
            env=env,
            runtime_home=runtime_home,
        )

    # ---------- 内部 ----------

    def _prepare_home(self) -> Path:
        """重建隔离 runtime home（清空旧内容，避免派生产物累积）。"""
        home = self._runtime_root / "pi-home"
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        (home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
        return home

    def _write_models_json(
        self, runtime_home: Path, decision: RoutingDecision, has_secret: bool
    ) -> None:
        endpoint = decision.endpoint
        model = decision.model

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

        provider["models"] = [model_entry]
        payload = {"providers": {decision.provider_key: provider}}

        target = runtime_home / ".pi" / "agent" / "models.json"
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target.write_text(body, encoding="utf-8")

    def _write_settings_json(self, runtime_home: Path) -> None:
        """硬化基线（ADR 第七节 / GATE-05）：关遥测、关项目信任、关内部重试。"""
        settings = {
            "enableInstallTelemetry": False,
            "defaultProjectTrust": "never",
            "retry": {"enabled": False, "provider": {"maxRetries": 0}},
        }
        target = runtime_home / ".pi" / "agent" / "settings.json"
        target.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
