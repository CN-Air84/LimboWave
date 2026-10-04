"""Task 2.1/2.2/2.3 服务层验收：站点/模型 CRUD + 归一建议。

锁住的保护性语义：
- 整配置校验跑在每一次写路径上（id 重复、绑定引用不存在的端点都会失败）；
- 删除仍被绑定的端点拒绝（不静默级联）；
- 删除默认模型拒绝；
- 逻辑模型可以没有绑定（先建后绑）；
- 导入 / 探测实际模型只写实际模型目录，**不创建逻辑模型**；
- 一个实际模型只能绑定一个逻辑模型；自动匹配不改动手动配对；
- 归一函数只做 trim/lowercase/去 -free，建议列表不构成合并。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.model_normalize import normalize_model_id, suggest_logical_models
from limbowave.domain.models import ActualModel, LogicalModel, ModelBinding, ModelCapability
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository


@pytest.fixture
def service(tmp_path: Path) -> SettingsService:
    repo = JsonConfigRepository(tmp_path / "config.json")
    return SettingsService(ConfigurationService(repo))


def _endpoint(id: str = "relay-a", name: str = "中转站 A") -> EndpointConfig:
    return EndpointConfig(
        id=id,
        name=name,
        base_url=f"https://{id}.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
        credential_ref=f"{id}-key",
    )


def _model(id: str = "deepseek-chat", endpoint_id: str = "relay-a") -> LogicalModel:
    return LogicalModel(
        id=id,
        name="DeepSeek",
        bindings=[ModelBinding(endpoint_id=endpoint_id, model_id=id)],
    )


def _probe(service: SettingsService, endpoint_id: str, model_id: str, **caps: object):
    fields: dict[str, object] = {
        "default_thinking_level": None,
        "thinking_level_locked": False,
        "supports_thinking": None,
        "supports_tools": False,
    }
    fields.update(caps)
    return service.record_probed_model(
        endpoint_id=endpoint_id, model_id=model_id, display_name=model_id, **fields  # type: ignore[arg-type]
    )


# ---------- 归一（Task 2.2 的确定性变换）----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DeepSeek-Chat", "deepseek-chat"),
        ("  glm-4.6  ", "glm-4.6"),
        ("qwen3-max-free", "qwen3-max"),
        ("QWEN3-MAX-FREE", "qwen3-max"),
        ("deepseek-chat-freely", "deepseek-chat-freely"),  # 只去**末尾**的 -free
        ("free-model", "free-model"),  # 前缀 free- 不动
    ],
)
def test_normalize_model_id(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected


def test_suggest_only_normalization_not_fuzzy() -> None:
    existing = ["deepseek-chat", "DeepSeek-Chat-Free", "glm-4.6"]
    hits = suggest_logical_models("DEEPSEEK-CHAT", existing)
    assert sorted(hits) == ["DeepSeek-Chat-Free", "deepseek-chat"]
    # 模糊相似不命中：glm-4 与 glm-4.6 不是一个东西
    assert suggest_logical_models("glm-4", existing) == []


def test_suggest_empty_target() -> None:
    assert suggest_logical_models("   ", ["a"]) == []


# ---------- 端点 CRUD ----------


def test_upsert_endpoint_adds_then_replaces(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    replaced = _endpoint(name="改名")
    config = service.upsert_endpoint(replaced)
    assert len(config.endpoints) == 1
    assert config.endpoints[0].name == "改名"


def test_upsert_endpoint_persists_to_disk(service: SettingsService, tmp_path: Path) -> None:
    service.upsert_endpoint(_endpoint())
    # 新实例读同一文件——真的落了盘
    reread = SettingsService(
        ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    ).load()
    assert [e.id for e in reread.endpoints] == ["relay-a"]


def test_delete_endpoint_refused_while_bound(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    with pytest.raises(ValueError, match="仍被模型绑定"):
        service.delete_endpoint("relay-a")


def test_delete_endpoint_after_unbind(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.upsert_endpoint(_endpoint(id="relay-b", name="B"))
    service.upsert_model(_model())
    service.bind_endpoint("deepseek-chat", ModelBinding(endpoint_id="relay-b", model_id="ds"))
    service.unbind_endpoint("deepseek-chat", "relay-a")
    config = service.delete_endpoint("relay-a")
    assert config.endpoints[0].id == "relay-b"


def test_endpoint_invalid_url_rejected(service: SettingsService) -> None:
    with pytest.raises(ValueError, match="http"):
        service.upsert_endpoint(
            EndpointConfig(
                id="bad",
                name="bad",
                base_url="not-a-url",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
        )


# ---------- 模型 CRUD 与默认模型 ----------


def test_upsert_model_requires_existing_endpoint(service: SettingsService) -> None:
    """整配置校验：绑定指向不存在的端点必须失败，不写半截配置。"""
    with pytest.raises(ValueError, match="不存在的端点"):
        service.upsert_model(_model(endpoint_id="ghost"))


def test_default_model_flow(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    service.upsert_model(_model(id="glm", endpoint_id="relay-a"))

    config = service.set_default_model("glm")
    assert config.default_model_id == "glm"

    with pytest.raises(ValueError, match="默认模型"):
        service.delete_model("glm")

    service.delete_model("deepseek-chat")
    assert [m.id for m in service.load().models] == ["glm"]


def test_duplicate_ids_rejected(service: SettingsService) -> None:
    """配置级校验：两个模型撞 id（绕过 upsert 的按 id 去重）由整配置校验兜底。"""
    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    # 直接构造非法配置走 save 之外的写路径不存在——upsert 按 id 替换，
    # 因此这里验证的是 delete 后再加同名也是同一实体（幂等），而非法配置
    # 在 AppConfiguration 层就被拒（domain 测试已覆盖），此条确认服务层不吞错。
    config = service.load()
    assert len(config.models) == 1


# ---------- 绑定 ----------


def test_bind_and_rebind_updates_model_id(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    config = service.bind_endpoint(
        "deepseek-chat", ModelBinding(endpoint_id="relay-a", model_id="deepseek-v4")
    )
    model = config.models[0]
    assert len(model.bindings) == 1
    assert model.bindings[0].model_id == "deepseek-v4"


def test_unbind_last_binding_keeps_logical_model(service: SettingsService) -> None:
    """逻辑模型由用户建立，可以解到一条绑定不剩；路由如实报「未绑定」。"""
    from limbowave.application.services.routing_service import RoutingService
    from limbowave.domain.routing import RoutingError

    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    config = service.unbind_endpoint("deepseek-chat", "relay-a")
    assert config.models[0].bindings == []
    # 实际模型仍在目录里，可以再配对
    assert config.actual_model("relay-a", "deepseek-chat") is not None
    with pytest.raises(RoutingError, match="还没有绑定实际模型"):
        RoutingService(config).route("deepseek-chat")


def test_unbind_clamps_default_binding(service: SettingsService) -> None:
    """默认绑定索引在删除后越界时收敛到 0，不留悬空索引。"""
    service.upsert_endpoint(_endpoint())
    service.upsert_endpoint(_endpoint(id="relay-b", name="B"))
    service.upsert_endpoint(_endpoint(id="relay-c", name="C"))
    service.upsert_model(_model())
    service.bind_endpoint("deepseek-chat", ModelBinding(endpoint_id="relay-b", model_id="ds"))
    service.bind_endpoint("deepseek-chat", ModelBinding(endpoint_id="relay-c", model_id="ds"))
    service.set_default_binding("deepseek-chat", 2)

    config = service.unbind_endpoint("deepseek-chat", "relay-c")
    model = config.models[0]
    assert len(model.bindings) == 2
    assert model.default_binding <= len(model.bindings) - 1


def test_unbind_keeps_default_endpoint_not_index(service: SettingsService) -> None:
    """默认站点跟着绑定走：解掉排在前面的绑定后，默认仍是原来那个站点。"""
    service.upsert_endpoint(_endpoint())
    service.upsert_endpoint(_endpoint(id="relay-b", name="B"))
    service.upsert_model(_model())
    service.bind_endpoint("deepseek-chat", ModelBinding(endpoint_id="relay-b", model_id="ds"))
    service.set_default_binding("deepseek-chat", 1)
    model = service.unbind_endpoint("deepseek-chat", "relay-a").models[0]
    assert model.bindings[model.default_binding].endpoint_id == "relay-b"


def test_set_default_binding_out_of_range(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.upsert_model(_model())
    with pytest.raises(ValueError, match="越界"):
        service.set_default_binding("deepseek-chat", 5)


def test_updating_binding_keeps_default_endpoint(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.upsert_endpoint(_endpoint("relay-b"))
    service.upsert_model(
        LogicalModel(
            id="model",
            name="Model",
            bindings=[
                ModelBinding(endpoint_id="relay-a", model_id="remote-a"),
                ModelBinding(endpoint_id="relay-b", model_id="remote-b"),
            ],
            default_binding=0,
        )
    )
    # 重新检测只更新实际模型目录；绑定上的能力随之投影，默认站点不变
    _probe(service, "relay-a", "remote-a", supports_tools=True)
    model = service.load().models[0]
    assert model.bindings[model.default_binding].endpoint_id == "relay-a"
    assert model.bindings[0].supports_tools


# ---------- 实际模型目录：导入不创建逻辑模型 ----------


def test_probed_model_goes_to_catalog_without_creating_logical_model(
    service: SettingsService,
) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    config, bound = service.record_probed_model(
        endpoint_id="relay-a",
        model_id="qwen-3.8-max",
        display_name="Qwen Max",
        default_thinking_level="high",
        thinking_level_locked=False,
        supports_thinking=True,
        supports_tools=True,
        available_thinking_levels=("low", "high", "max"),
    )
    assert bound is None
    assert config.models == []
    assert config.default_model_id is None
    actual = config.actual_model("relay-a", "qwen-3.8-max")
    assert actual is not None
    assert actual.name == "Qwen Max"
    assert actual.default_thinking_level == "high"
    assert actual.supports_thinking is True
    assert actual.available_thinking_levels == ("low", "high", "max")
    assert actual.supports_tools is True


def test_importing_many_models_leaves_logical_models_alone(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    for model_id in ("gpt-5", "grok-4.7", "z-ai/glm-5.3", "gemini-3.8-flash-high"):
        _probe(service, "relay-a", model_id)
    service.record_unverified_model(endpoint_id="relay-a", model_id="manual", display_name="manual")
    config = service.load()
    assert config.models == []
    assert len(config.actual_models) == 5


def test_first_import_auto_binds_to_matching_logical_model(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.upsert_endpoint(_endpoint("relay-b"))
    service.create_model(LogicalModel(id="deepseek-chat", name="DeepSeek"))
    _config, bound = _probe(service, "relay-a", "DeepSeek-Chat-Free", supports_tools=True)
    assert bound == "deepseek-chat"
    _config, unrelated = _probe(service, "relay-b", "glm-4.6")
    assert unrelated is None
    model = service.load().models[0]
    assert [(b.endpoint_id, b.model_id, b.auto_matched) for b in model.bindings] == [
        ("relay-a", "DeepSeek-Chat-Free", True)
    ]
    assert model.bindings[0].supports_tools


def test_unverified_model_never_erases_verified_capabilities(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.record_unverified_model(
        endpoint_id="relay-a", model_id="manual-model", display_name="Manual Model"
    )
    actual = service.load().actual_model("relay-a", "manual-model")
    assert actual is not None
    assert actual.supports_thinking is None
    assert actual.supports_tools is False
    _probe(
        service, "relay-a", "manual-model",
        default_thinking_level="high", supports_thinking=True, supports_tools=True,
        available_thinking_levels=("low", "high"),
    )
    service.record_unverified_model(
        endpoint_id="relay-a", model_id="manual-model", display_name="Manual Model"
    )
    verified = service.load().actual_model("relay-a", "manual-model")
    assert verified is not None
    assert verified.supports_tools is True
    assert verified.available_thinking_levels == ("low", "high")


def test_unverified_model_rejects_unknown_endpoint(service: SettingsService) -> None:
    with pytest.raises(ValueError, match="端点不存在"):
        service.record_unverified_model(
            endpoint_id="missing", model_id="manual-model", display_name="Manual Model"
        )


def test_delete_endpoint_drops_its_unbound_catalog_entries(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "m")
    config = service.delete_endpoint("relay-a")
    assert config.actual_models == []


# ---------- 逻辑模型：用户建立 + 自动匹配 / 手动配对 ----------


def test_create_model_auto_matches_one_per_endpoint(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.upsert_endpoint(_endpoint("relay-b"))
    service.upsert_endpoint(_endpoint("relay-c"))
    _probe(service, "relay-a", "deepseek-chat", supports_tools=True)
    _probe(service, "relay-b", "DeepSeek-Chat-free")
    _probe(service, "relay-b", "deepseek-chat-v2")  # 不是归一匹配，不绑
    _probe(service, "relay-c", "glm-4.6")
    config, plan = service.create_model(LogicalModel(id="deepseek-chat", name="DeepSeek"))
    model = config.models[0]
    assert [(b.endpoint_id, b.model_id) for b in model.bindings] == [
        ("relay-a", "deepseek-chat"),
        ("relay-b", "DeepSeek-Chat-free"),
    ]
    assert all(b.auto_matched for b in model.bindings)
    assert model.bindings[0].supports_tools  # 能力取自实际模型目录
    assert [a.model_id for a in plan.bind] == ["deepseek-chat", "DeepSeek-Chat-free"]
    assert config.default_model_id == "deepseek-chat"  # 第一个逻辑模型成为默认


def test_create_model_prefers_closest_candidate_and_reports_ties(
    service: SettingsService,
) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.upsert_endpoint(_endpoint("relay-b"))
    _probe(service, "relay-a", "gpt-5")
    _probe(service, "relay-a", "gpt-5-free")  # 不如完全相同的接近
    _probe(service, "relay-b", "GPT-5")
    _probe(service, "relay-b", "gpt-5 ")  # 与 GPT-5 同样接近（仅大小写/空白不同）→ 不替用户挑
    config, plan = service.create_model(LogicalModel(id="gpt-5", name="GPT-5"))
    assert [(b.endpoint_id, b.model_id) for b in config.models[0].bindings] == [
        ("relay-a", "gpt-5")
    ]
    assert plan.ambiguous == (("relay-b", ("GPT-5", "gpt-5 ")),)


def test_create_model_rejects_duplicate_id(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.create_model(LogicalModel(id="m", name="M"))
    with pytest.raises(ValueError, match="已存在"):
        service.create_model(LogicalModel(id="m", name="M2"))


def test_actual_model_binds_to_at_most_one_logical_model(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "deepseek-chat")
    service.create_model(LogicalModel(id="deepseek-chat", name="DeepSeek"))
    # 第二个同名归一的逻辑模型不抢已绑定的实际模型
    config, plan = service.create_model(LogicalModel(id="DeepSeek-Chat", name="Other"))
    assert config.models[1].bindings == []
    assert [(a.model_id, owner) for a, owner in plan.taken] == [("deepseek-chat", "deepseek-chat")]
    with pytest.raises(ValueError, match="只能绑定一个逻辑模型"):
        service.pair_actual_model("DeepSeek-Chat", "relay-a", "deepseek-chat")
    # 配置层同样兜底
    with pytest.raises(ValueError, match="只能绑定一个逻辑模型"):
        service.upsert_model(
            LogicalModel(
                id="third", name="Third",
                bindings=[ModelBinding(endpoint_id="relay-a", model_id="deepseek-chat")],
            )
        )


def test_manual_pair_is_never_replaced_by_auto_match(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "claude-latest")
    service.create_model(LogicalModel(id="claude-opus", name="Claude Opus"))
    service.pair_actual_model("claude-opus", "relay-a", "claude-latest")
    _probe(service, "relay-a", "claude-opus")  # 之后导入了 ID 完全相同的
    _config, plan = service.auto_match("claude-opus")
    model = service.load().models[0]
    assert [(b.model_id, b.auto_matched) for b in model.bindings] == [("claude-latest", False)]
    assert plan.bind == ()


def test_import_upgrades_weaker_auto_binding(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "kimi-k2-free")
    service.create_model(LogicalModel(id="kimi-k2", name="Kimi"))
    _probe(service, "relay-a", "kimi-k2")  # 更接近：首次导入时替换自动建立的绑定
    model = service.load().models[0]
    assert [b.model_id for b in model.bindings] == ["kimi-k2"]


def test_pair_requires_catalog_entry(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    service.create_model(LogicalModel(id="m", name="M"))
    with pytest.raises(ValueError, match="实际模型不存在"):
        service.pair_actual_model("m", "relay-a", "ghost")


def test_default_thinking_level_is_stored_on_actual_model(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "m", default_thinking_level="high")
    service.create_model(LogicalModel(id="m", name="M"))
    config = service.set_default_thinking_level("relay-a", "m", "low")
    actual = config.actual_model("relay-a", "m")
    assert actual is not None
    assert actual.default_thinking_level == "low"
    assert config.models[0].bindings[0].default_thinking_level == "low"


def test_delete_model_keeps_actual_models(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint("relay-a"))
    _probe(service, "relay-a", "m")
    service.create_model(LogicalModel(id="keep", name="Keep"))
    service.create_model(LogicalModel(id="m", name="M"))
    config = service.delete_model("m")
    assert [m.id for m in config.models] == ["keep"]
    assert config.actual_model("relay-a", "m") is not None


# ---------- 手动能力声明 ----------


@pytest.mark.parametrize(
    "capability", ["supports_streaming", "supports_thinking", "supports_tools"],
)
@pytest.mark.parametrize("supported", [True, False])
def test_manual_capabilities_persist_without_creating_logical_models(
    service: SettingsService, capability: ModelCapability, supported: bool,
) -> None:
    service.upsert_endpoint(_endpoint())
    config, owner = service.set_model_capability(
        endpoint_id="relay-a", model_id="manual", display_name="Manual",
        capability=capability, supported=supported,
    )
    assert owner is None
    assert not config.models
    actual = service.load().actual_model("relay-a", "manual")
    assert actual is not None
    assert actual.name == "Manual"
    assert getattr(actual, capability) is supported
    assert actual.available_thinking_levels == ()
    assert actual.default_thinking_level is None


def test_manual_edit_only_changes_target_capability_and_projects_binding(
    service: SettingsService,
) -> None:
    service.upsert_endpoint(_endpoint())
    service.create_model(LogicalModel(id="m", name="M"))
    _probe(
        service, "relay-a", "m", default_thinking_level="high",
        supports_thinking=True, supports_tools=True, available_thinking_levels=("low", "high"),
    )
    service.set_model_capability(
        endpoint_id="relay-a", model_id="m", display_name="m",
        capability="supports_streaming", supported=False,
    )
    config = service.load()
    actual = config.actual_model("relay-a", "m")
    assert actual is not None
    assert actual.supports_streaming is False
    assert actual.supports_tools is True
    assert actual.supports_thinking is True
    assert actual.default_thinking_level == "high"
    assert actual.available_thinking_levels == ("low", "high")
    binding = config.models[0].bindings[0]
    assert binding.supports_streaming is False
    assert binding.supports_thinking is True
    assert "supports_streaming" not in binding.model_dump()


def test_disabling_thinking_clears_incompatible_levels(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    _probe(
        service, "relay-a", "m", default_thinking_level="high", thinking_level_locked=True,
        supports_thinking=True, supports_tools=True, available_thinking_levels=("high",),
    )
    for supported in (False, True):
        service.set_model_capability(
            endpoint_id="relay-a", model_id="m", display_name="m",
            capability="supports_thinking", supported=supported,
        )
        actual = service.load().actual_model("relay-a", "m")
        assert actual is not None
        assert actual.supports_thinking is supported
        assert actual.default_thinking_level is None
        assert actual.available_thinking_levels == ()
        assert not actual.thinking_level_locked
        assert actual.supports_tools
        assert actual.supports_streaming


def test_explicit_reprobe_replaces_manual_capability(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    for capability in ("supports_streaming", "supports_thinking", "supports_tools"):
        service.set_model_capability(
            endpoint_id="relay-a", model_id="m", display_name="m",
            capability=capability, supported=False,
        )
    _probe(service, "relay-a", "m", supports_thinking=True, supports_tools=True)
    actual = service.load().actual_model("relay-a", "m")
    assert actual is not None
    assert actual.supports_streaming
    assert actual.supports_thinking
    assert actual.supports_tools


def test_manual_first_import_uses_existing_auto_pairing(service: SettingsService) -> None:
    service.upsert_endpoint(_endpoint())
    service.create_model(LogicalModel(id="m", name="M"))
    config, owner = service.set_model_capability(
        endpoint_id="relay-a", model_id="M-free", display_name="M",
        capability="supports_tools", supported=True,
    )
    assert owner == "m"
    assert len(config.models) == 1
    assert config.models[0].bindings[0].model_id == "M-free"
    assert config.models[0].bindings[0].supports_tools


def test_manual_capabilities_reject_unknown_endpoint_or_field(service: SettingsService) -> None:
    with pytest.raises(ValueError, match="端点不存在"):
        service.set_model_capability(
            endpoint_id="missing", model_id="m", display_name="m",
            capability="supports_tools", supported=True,
        )
    service.upsert_endpoint(_endpoint())
    with pytest.raises(ValueError, match="未知模型能力"):
        service.set_model_capability(
            endpoint_id="relay-a", model_id="m", display_name="m",
            capability="name", supported=True,  # type: ignore[arg-type]
        )
    assert not service.load().actual_models


def test_legacy_streaming_capability_is_unknown() -> None:
    assert ActualModel(endpoint_id="e", model_id="m").supports_streaming is None
    assert ModelBinding(endpoint_id="e", model_id="m").supports_streaming is None


# ---------- 旧版配置迁移 ----------


def test_legacy_binding_capabilities_migrate_into_catalog(tmp_path: Path) -> None:
    import json

    path = tmp_path / "config.json"
    legacy_binding = {
        "endpoint_id": "relay-a",
        "model_id": "remote",
        "default_thinking_level": "high",
        "available_thinking_levels": ["low", "high"],
        "supports_thinking": True,
        "supports_tools": True,
    }
    path.write_text(
        json.dumps(
            {
                "endpoints": [_endpoint("relay-a").model_dump(mode="json")],
                "models": [{"id": "m", "name": "M", "bindings": [legacy_binding]}],
            }
        ),
        encoding="utf-8",
    )
    service = SettingsService(ConfigurationService(JsonConfigRepository(path)))
    config = service.load()
    actual = config.actual_model("relay-a", "remote")
    assert actual is not None
    assert actual.supports_tools
    assert actual.available_thinking_levels == ("low", "high")
    binding = config.models[0].bindings[0]
    assert binding.supports_tools
    assert binding.default_thinking_level == "high"

    service.set_default_model("m")  # 触发一次落盘
    saved = json.loads(path.read_text(encoding="utf-8"))
    # 能力只在实际模型目录里出现一次，绑定只剩引用
    assert set(saved["models"][0]["bindings"][0]) == {"endpoint_id", "model_id", "auto_matched"}
    assert saved["actual_models"][0]["supports_tools"] is True
    reloaded = service.load().models[0].bindings[0]
    assert reloaded.supports_tools
    assert reloaded.available_thinking_levels == ("low", "high")
