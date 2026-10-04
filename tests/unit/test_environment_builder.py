"""Pi 运行环境构建器的单元测试。

关键不变量（Phase 1A 验收 + ADR 裁决 3）：
- Pi 的 models.json 是**运行时派生产物**，密钥只以 ``$ENV`` 引用出现。
- 运行环境隔离到自己的 runtime home，不触碰真实 ``~/.pi``。
- 硬化基线：关遥测、关项目信任、关内部重试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.application.services.routing_service import RoutingService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.routing import RoutingError
from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder

SECRET = "sk-live-SHOULD-NOT-APPEAR-IN-PI-CONFIG"


def _config(*, credential_ref: str | None = "key-a") -> AppConfiguration:
    return AppConfiguration(
        endpoints=[
            EndpointConfig(
                id="relay-a",
                name="中转 A",
                base_url="https://relay.example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
                credential_ref=credential_ref,
                headers={"X-Tenant": "demo"},
                compat={"supportsDeveloperRole": False},
            )
        ],
        models=[
            LogicalModel(
                id="deepseek-chat",
                name="DeepSeek Chat",
                bindings=[ModelBinding(endpoint_id="relay-a", model_id="deepseek-chat")],
                context_window=128000,
                max_tokens=4096,
                supports_images=True,
            )
        ],
    )


def _build(tmp_path: Path, *, credential_ref: str | None = "key-a", secret: str | None = SECRET):
    decision = RoutingService(_config(credential_ref=credential_ref)).route()
    return EnvironmentBuilder(tmp_path).build(decision, secret)


def _models_json(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "pi-home" / ".pi" / "agent" / "models.json").read_text("utf-8"))


def _settings_json(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "pi-home" / ".pi" / "agent" / "settings.json").read_text("utf-8"))


def test_provider_and_model_keys(tmp_path: Path) -> None:
    env = _build(tmp_path)
    assert env.provider_key == "relay-a"
    assert env.model_id == "deepseek-chat"


def test_models_json_is_derived_with_dollar_env_ref(tmp_path: Path) -> None:
    _build(tmp_path)
    provider = _models_json(tmp_path)["providers"]["relay-a"]

    assert provider["baseUrl"] == "https://relay.example.com/v1"
    assert provider["api"] == "openai-completions"
    # 密钥只以 $ENV 引用出现，绝不写明文
    assert provider["apiKey"] == "$LIMBOWAVE_SECRET_KEY_A"
    assert provider["authHeader"] is True
    assert provider["headers"] == {"X-Tenant": "demo"}
    assert provider["compat"] == {"supportsDeveloperRole": False}


def test_secret_plaintext_not_in_pi_config(tmp_path: Path) -> None:
    """整个 runtime home 里不得出现密钥明文。"""
    _build(tmp_path)
    for path in (tmp_path / "pi-home").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


def test_model_capabilities_derived(tmp_path: Path) -> None:
    _build(tmp_path)
    entry = _models_json(tmp_path)["providers"]["relay-a"]["models"][0]
    assert entry["id"] == "deepseek-chat"
    assert entry["contextWindow"] == 128000
    assert entry["maxTokens"] == 4096
    assert entry["input"] == ["text", "image"]


def test_settings_hardened(tmp_path: Path) -> None:
    """硬化基线：关遥测、关项目信任、关内部重试（ADR 第七节 / GATE-05）。"""
    _build(tmp_path)
    settings = _settings_json(tmp_path)
    assert settings["enableInstallTelemetry"] is False
    assert settings["defaultProjectTrust"] == "never"
    assert settings["retry"] == {"enabled": False, "provider": {"maxRetries": 0}}


def test_env_isolates_home_and_injects_secret(tmp_path: Path) -> None:
    env = _build(tmp_path)
    home = str(tmp_path / "pi-home")
    # 隔离 Pi 的配置目录（Node 的 os.homedir() 在 Windows 优先 USERPROFILE）
    assert env.env["USERPROFILE"] == home
    assert env.env["HOME"] == home
    assert env.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert env.env["PI_OFFLINE"] == "1"
    # 密钥只经环境变量进入子进程
    assert env.env["LIMBOWAVE_SECRET_KEY_A"] == SECRET


def test_credential_ref_without_secret_raises(tmp_path: Path) -> None:
    """端点引用了凭据但库里没有：明确失败，不静默发未认证请求。"""
    with pytest.raises(RoutingError, match="不存在"):
        _build(tmp_path, credential_ref="key-a", secret=None)


def test_no_credential_ref_omits_api_key(tmp_path: Path) -> None:
    """本地端点（如 Ollama）无密钥时不得写 apiKey 字段。"""
    _build(tmp_path, credential_ref=None, secret=None)
    provider = _models_json(tmp_path)["providers"]["relay-a"]
    assert "apiKey" not in provider
    assert "authHeader" not in provider


def test_rebuild_does_not_accumulate(tmp_path: Path) -> None:
    """重复构建应重建 runtime home，不留旧派生产物。"""
    _build(tmp_path)
    stale = tmp_path / "pi-home" / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    _build(tmp_path)
    assert not stale.exists()


def test_detected_reasoning_capability_is_derived(tmp_path: Path) -> None:
    config = _config()
    model = config.models[0].model_copy(
        update={
            "bindings": [
                config.models[0].bindings[0].model_copy(update={"supports_thinking": True})
            ]
        }
    )
    decision = RoutingService(config.model_copy(update={"models": [model]})).route()
    EnvironmentBuilder(tmp_path).build(decision, SECRET)
    entry = _models_json(tmp_path)["providers"]["relay-a"]["models"][0]
    assert entry["reasoning"] is True


def _multi_config() -> AppConfiguration:
    """两个站点、两个逻辑模型：会话内切换逻辑模型/站点的场景。"""
    base = _config()
    relay_b = EndpointConfig(
        id="relay-b",
        name="中转 B",
        base_url="https://relay-b.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
        credential_ref="key-b",
        strip_params=("reasoning_effort",),
    )
    gemini = LogicalModel(
        id="gemini",
        name="Gemini",
        bindings=[
            ModelBinding(endpoint_id="relay-b", model_id="gemini-flash"),
            ModelBinding(endpoint_id="relay-a", model_id="gemini-flash-a"),
        ],
    )
    return base.model_copy(
        update={
            "endpoints": [*base.endpoints, relay_b],
            "models": [*base.models, gemini],
        }
    )


def test_catalog_registers_every_switchable_model(tmp_path: Path) -> None:
    """回归：只登记启动模型时，set_model 切到其他逻辑模型报 Model not found。"""
    routing = RoutingService(_multi_config())
    secrets = {"key-a": SECRET, "key-b": "sk-b"}
    catalog = [(d, secrets.get(d.endpoint.credential_ref or "")) for d in routing.catalog()]
    env = EnvironmentBuilder(tmp_path).build(routing.route(), SECRET, catalog=catalog)

    providers = _models_json(tmp_path)["providers"]
    assert [m["id"] for m in providers["relay-a"]["models"]] == ["deepseek-chat", "gemini-flash-a"]
    assert [m["id"] for m in providers["relay-b"]["models"]] == ["gemini-flash"]
    assert providers["relay-b"]["apiKey"] == "$LIMBOWAVE_SECRET_KEY_B"
    assert env.env["LIMBOWAVE_SECRET_KEY_B"] == "sk-b"
    # 启动模型不变
    assert (env.provider_key, env.model_id) == ("relay-a", "deepseek-chat")
    # 参数规则按 provider 分组，扩展按当前模型的 provider 取用
    assert json.loads(env.env["LIMBOWAVE_PARAM_RULES"]) == {
        "relay-b": {"whitelist": [], "strip": ["reasoning_effort"]}
    }


def test_catalog_entry_without_secret_is_skipped(tmp_path: Path) -> None:
    """备选站点缺凭据：跳过该站点，不拖垮主内核，也不发未认证请求。"""
    routing = RoutingService(_multi_config())
    catalog = [(d, SECRET if d.endpoint.id == "relay-a" else None) for d in routing.catalog()]
    env = EnvironmentBuilder(tmp_path).build(routing.route(), SECRET, catalog=catalog)

    providers = _models_json(tmp_path)["providers"]
    assert "relay-b" not in providers
    assert "LIMBOWAVE_SECRET_KEY_B" not in env.env
    assert "LIMBOWAVE_PARAM_RULES" not in env.env


def test_refresh_catalog_rewrites_in_place(tmp_path: Path) -> None:
    """热更新：就地重写 models.json（不清空 runtime home），给出要同步进进程的环境。"""
    builder = EnvironmentBuilder(tmp_path)
    builder.build(RoutingService(_config()).route(), SECRET)
    keep = tmp_path / "pi-home" / ".pi" / "agent" / "settings.json"

    routing = RoutingService(_multi_config())
    secrets = {"key-a": SECRET, "key-b": "sk-b"}
    catalog = [(d, secrets.get(d.endpoint.credential_ref or "")) for d in routing.catalog()]
    sync = builder.refresh_catalog(catalog)

    assert keep.exists()
    assert set(_models_json(tmp_path)["providers"]) == {"relay-a", "relay-b"}
    assert sync.env["LIMBOWAVE_SECRET_KEY_A"] == SECRET
    assert sync.env["LIMBOWAVE_SECRET_KEY_B"] == "sk-b"
    assert json.loads(sync.env["LIMBOWAVE_PARAM_RULES"]) == {
        "relay-b": {"whitelist": [], "strip": ["reasoning_effort"]}
    }
    assert sync.registered == (
        ("relay-a", "deepseek-chat"),
        ("relay-b", "gemini-flash"),
        ("relay-a", "gemini-flash-a"),
    )
    # 内容不变 → 摘要不变（可跳过热更新）；换密钥 → 摘要变
    assert builder.refresh_catalog(catalog).fingerprint == sync.fingerprint
    rotated = [(d, "sk-rotated" if s == "sk-b" else s) for d, s in catalog]
    assert builder.refresh_catalog(rotated).fingerprint != sync.fingerprint


def test_refresh_catalog_skips_unchanged_write(tmp_path: Path) -> None:
    """内容未变时不落盘：发送前每次都要热同步，反复重写 models.json 既无谓
    又可能与 Pi 读文件抢。指纹仍稳定；内容真变了才动文件。

    回归守卫：会话启动后补的能力声明（视觉）要靠「发送前同步」补进 Pi，
    而那条路每次发送都会调 refresh_catalog，必须廉价。
    """
    builder = EnvironmentBuilder(tmp_path)
    routing = RoutingService(_multi_config())
    secrets = {"key-a": SECRET, "key-b": "sk-b"}
    catalog = [(d, secrets.get(d.endpoint.credential_ref or "")) for d in routing.catalog()]
    builder.refresh_catalog(catalog)
    models_json = tmp_path / "pi-home" / ".pi" / "agent" / "models.json"
    first = models_json.read_text(encoding="utf-8")
    mtime_before = models_json.stat().st_mtime_ns

    # 同一份目录再同步：文件原样不动。
    assert builder.refresh_catalog(catalog).fingerprint is not None
    assert models_json.read_text(encoding="utf-8") == first
    assert models_json.stat().st_mtime_ns == mtime_before

    # 内容真的变了（拿掉一条模型）→ 必须重写。
    single = [(d, s) for d, s in catalog if d.model_id != "gemini-flash"]
    builder.refresh_catalog(single)
    assert models_json.read_text(encoding="utf-8") != first


def test_refresh_catalog_clears_param_rules(tmp_path: Path) -> None:
    """没有规则时也要下发空值——否则进程里残留的旧规则会继续改写请求。"""
    routing = RoutingService(_config())
    sync = EnvironmentBuilder(tmp_path).refresh_catalog([(routing.route(), SECRET)])
    assert sync.env["LIMBOWAVE_PARAM_RULES"] == ""
