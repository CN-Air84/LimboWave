"""站点、实际模型与逻辑模型管理服务（Task 2.1 / 2.2 / 2.3 的应用层入口）。

在 :class:`ConfigurationService` 之上提供**面向操作的** CRUD：
每个操作产出一个新的 ``AppConfiguration``（领域对象不可变），经仓库落盘。

三类实体的分工：

- **实际模型**（目录）：站点模型清单导入 / 能力探测 / 手动输入。导入**不创建逻辑模型**，
  只在首次导入时按 ID 自动归入唯一最接近的已有逻辑模型；
- **逻辑模型**：只由用户建立（ID 手输，或引用实际模型 ID），建立时按 ID 自动匹配；
- **绑定**：自动匹配或手动配对。一个实际模型只能绑定一个逻辑模型；
  同一逻辑模型在一个站点至多一条绑定。

保护性语义（都是有意的取舍，不是缺漏）：

- **删除端点前拒绝**：仍有模型绑定它的端点不得删除——绑定是用户的显式决定，
  不能静默级联（该站点未绑定的实际模型目录项随端点一起删除）。
- **删除默认模型前拒绝**：先把默认指向别处，再删。
- **手动配对不抢占**：实际模型已绑定到别的逻辑模型时拒绝，先在那边解绑。
- 所有写路径都**显式重建** ``AppConfiguration``，因此整配置校验
  （id 唯一、引用有效、一个实际模型只绑一个逻辑模型）
  必然执行——校验失败抛 ``ValueError``，不写半截配置。

**密钥红线**：本服务只碰 ``credential_ref`` 引用名；密钥本体走
:class:`CredentialService`，不进配置、不进日志。
"""

from __future__ import annotations

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.model_matching import MatchPlan, logical_for_actual, plan_for_logical
from limbowave.domain.models import ActualModel, LogicalModel, ModelBinding, ModelCapability
from limbowave.domain.providers import EndpointConfig

_KEEP = object()  # 哨兵：default_model_id 不传 = 保持原值；显式传 None = 清除


def _rebuild(
    config: AppConfiguration,
    *,
    endpoints: list[EndpointConfig] | None = None,
    actual_models: list[ActualModel] | None = None,
    models: list[LogicalModel] | None = None,
    default_model_id: str | object | None = _KEEP,
) -> AppConfiguration:
    """显式重建配置，让 pydantic 校验跑在每一次写路径上。"""
    return AppConfiguration(
        endpoints=config.endpoints if endpoints is None else endpoints,
        actual_models=config.actual_models if actual_models is None else actual_models,
        models=config.models if models is None else models,
        default_model_id=(
            config.default_model_id if default_model_id is _KEEP else default_model_id  # type: ignore[arg-type]
        ),
        compression=config.compression,
        memory=config.memory,
    )


def _replace_model(
    config: AppConfiguration, model_id: str, updated: LogicalModel
) -> list[LogicalModel]:
    return [updated if m.id == model_id else m for m in config.models]


def _with_binding(model: LogicalModel, binding: ModelBinding) -> LogicalModel:
    """同站点已有绑定则原位替换（备用顺序与默认站点不变），否则追加到末尾。"""
    if any(b.endpoint_id == binding.endpoint_id for b in model.bindings):
        bindings = [binding if b.endpoint_id == binding.endpoint_id else b for b in model.bindings]
    else:
        bindings = [*model.bindings, binding]
    return _rebuild_model(model, bindings=bindings)


def _apply_plan(config: AppConfiguration, plan: MatchPlan) -> list[LogicalModel]:
    model = _require_model(config, plan.logical_id)
    for actual in plan.bind:
        model = _with_binding(
            model,
            ModelBinding(
                endpoint_id=actual.endpoint_id, model_id=actual.model_id, auto_matched=True
            ),
        )
    return _replace_model(config, plan.logical_id, model)


def _display_name(display_name: str, model_id: str) -> str:
    """只在清单给出的显示名与模型 ID 不同时才记录（空 = 直接显示 ID）。"""
    name = display_name.strip()
    return "" if name == model_id else name


class SettingsService:
    """站点端点、实际模型与逻辑模型的 CRUD。每次写操作 = 加载 → 变换 → 校验 → 保存。"""

    def __init__(self, configuration: ConfigurationService) -> None:
        self._configuration = configuration

    # ---------- 读 ----------

    def load(self) -> AppConfiguration:
        return self._configuration.load()

    def _save(self, config: AppConfiguration) -> AppConfiguration:
        self._configuration.save(config)
        return config

    # ---------- 站点端点 ----------

    def upsert_endpoint(self, endpoint: EndpointConfig) -> AppConfiguration:
        """新增或按 id 替换一个端点。"""
        config = self.load()
        endpoints = [e for e in config.endpoints if e.id != endpoint.id]
        endpoints.append(endpoint)
        return self._save(_rebuild(config, endpoints=endpoints))

    def delete_endpoint(
        self, endpoint_id: str, *, unbind_models: bool = False
    ) -> AppConfiguration:
        """删除端点；仅在用户明确确认时批量解绑，一次校验并保存完整配置。

        默认仍拒绝删除已绑定的端点。解绑保留逻辑模型与其他站点的默认路由。
        """
        config = self.load()
        bound_by = [
            model.id
            for model in config.models
            if any(b.endpoint_id == endpoint_id for b in model.bindings)
        ]
        if bound_by and not unbind_models:
            raise ValueError(
                f"端点 {endpoint_id} 仍被模型绑定：{', '.join(bound_by)}。请先解除绑定。"
            )
        updated = _rebuild(
            config,
            endpoints=[e for e in config.endpoints if e.id != endpoint_id],
            actual_models=[a for a in config.actual_models if a.endpoint_id != endpoint_id],
            models=[_without_endpoint(model, endpoint_id) for model in config.models],
        )
        return self._save(updated)

    # ---------- 实际模型目录 ----------

    def record_probed_model(
        self,
        *,
        endpoint_id: str,
        model_id: str,
        display_name: str,
        default_thinking_level: str | None,
        thinking_level_locked: bool,
        supports_thinking: bool | None,
        supports_tools: bool,
        supports_streaming: bool = True,
        available_thinking_levels: tuple[str, ...] = (),
    ) -> tuple[AppConfiguration, str | None]:
        """把实际模型的探测结果写进目录——**不创建逻辑模型**。

        已在目录里：只更新能力（绑定上的投影随之更新）；首次导入：按 ID 自动归入
        唯一最接近的已有逻辑模型。返回 (新配置, 自动归入的逻辑模型 id 或 None)。
        """
        previous = self.load().actual_model(endpoint_id, model_id)
        user_levels = previous.user_thinking_levels if previous else ()
        return self._record(
            ActualModel(
                endpoint_id=endpoint_id,
                model_id=model_id,
                name=_display_name(display_name, model_id),
                user_thinking_levels=user_levels,
                supports_streaming=supports_streaming,
                default_thinking_level=default_thinking_level,
                thinking_level_locked=thinking_level_locked,
                available_thinking_levels=tuple(dict.fromkeys((
                    *available_thinking_levels, *user_levels,
                ))),
                supports_thinking=(
                    True if any(level != "off" for level in user_levels)
                    else supports_thinking
                ),
                supports_tools=supports_tools,
            )
        )

    def set_model_capability(
        self,
        *,
        endpoint_id: str,
        model_id: str,
        display_name: str,
        capability: ModelCapability,
        supported: bool,
    ) -> tuple[AppConfiguration, str | None]:
        """只更新用户修改的能力；未导入的模型沿用目录登记与自动配对规则。"""
        if capability not in ("supports_streaming", "supports_thinking", "supports_tools"):
            raise ValueError(f"未知模型能力：{capability}")
        config = self.load()
        _require_endpoint(config, endpoint_id)
        actual = config.actual_model(endpoint_id, model_id) or ActualModel(
            endpoint_id=endpoint_id,
            model_id=model_id,
            name=_display_name(display_name, model_id),
        )
        fields = {**actual.model_dump(), capability: supported}
        if capability == "supports_thinking" and not supported:
            fields.update(
                default_thinking_level=None,
                thinking_level_locked=False,
                available_thinking_levels=(),
                user_thinking_levels=(),
            )
        return self._record(ActualModel.model_validate(fields))

    def confirm_thinking_level(
        self, endpoint_id: str, model_id: str, level: str
    ) -> AppConfiguration:
        """仅在用户确认后合并该站点实际模型的单个等级，不覆盖其他能力。"""
        if level not in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError(f"未知思考等级：{level}")
        config = self.load()
        _require_endpoint(config, endpoint_id)
        actual = config.actual_model(endpoint_id, model_id)
        if actual is None:
            raise ValueError("实际模型已被删除，不能标记思考能力")
        updated = actual.model_copy(update={
            "supports_thinking": actual.supports_thinking if level == "off" else True,
            "thinking_level_locked": False,
            "available_thinking_levels": tuple(dict.fromkeys((
                *actual.available_thinking_levels, level,
            ))),
            "user_thinking_levels": tuple(dict.fromkeys((*actual.user_thinking_levels, level))),
        })
        return self._record(updated)[0]

    def record_unverified_model(
        self, *, endpoint_id: str, model_id: str, display_name: str
    ) -> tuple[AppConfiguration, str | None]:
        """模型清单 / 测活不可用时手动登记；**绝不覆盖**已有的能力探测结果。"""
        config = self.load()
        _require_endpoint(config, endpoint_id)
        if config.actual_model(endpoint_id, model_id) is not None:
            return config, None
        return self._record(
            ActualModel(
                endpoint_id=endpoint_id,
                model_id=model_id,
                name=_display_name(display_name, model_id),
            )
        )

    def _record(self, actual: ActualModel) -> tuple[AppConfiguration, str | None]:
        config = self.load()
        _require_endpoint(config, actual.endpoint_id)
        existing = config.actual_model(actual.endpoint_id, actual.model_id)
        if existing is not None:
            refreshed = actual.model_copy(update={"name": actual.name or existing.name})
            catalog = [refreshed if a.key == actual.key else a for a in config.actual_models]
            return self._save(_rebuild(config, actual_models=catalog)), None

        staged = _rebuild(config, actual_models=[*config.actual_models, actual])
        logical_id = logical_for_actual(staged, actual)
        if logical_id is None:
            return self._save(staged), None
        model = _with_binding(
            _require_model(staged, logical_id),
            ModelBinding(
                endpoint_id=actual.endpoint_id, model_id=actual.model_id, auto_matched=True
            ),
        )
        updated = _rebuild(staged, models=_replace_model(staged, logical_id, model))
        return self._save(updated), logical_id

    def set_default_thinking_level(
        self, endpoint_id: str, model_id: str, level: str | None
    ) -> AppConfiguration:
        """实际模型的默认思考强度（§二.4 模型级覆盖；路由到它时应用）。"""
        config = self.load()
        actual = config.actual_model(endpoint_id, model_id)
        if actual is None:
            raise ValueError(f"实际模型不存在：{endpoint_id} · {model_id}")
        updated_actual = ActualModel.model_validate(
            {**actual.model_dump(), "default_thinking_level": level}
        )
        catalog = [updated_actual if a.key == actual.key else a for a in config.actual_models]
        return self._save(_rebuild(config, actual_models=catalog))

    # ---------- 逻辑模型 ----------

    def create_model(self, model: LogicalModel) -> tuple[AppConfiguration, MatchPlan]:
        """新建逻辑模型（用户手动建立），并按 ID 自动匹配实际模型作为初始归类。

        ``model.bindings`` 可以为空；自动匹配只补没有手动绑定的站点。
        还没有默认模型时，新建的这个成为默认模型。
        """
        config = self.load()
        if any(m.id == model.id for m in config.models):
            raise ValueError(f"逻辑模型 ID 已存在：{model.id}")
        staged = _rebuild(
            config,
            models=[*config.models, model],
            default_model_id=config.default_model_id or model.id,
        )
        plan = plan_for_logical(staged, model.id)
        return self._save(_rebuild(staged, models=_apply_plan(staged, plan))), plan

    def upsert_model(self, model: LogicalModel) -> AppConfiguration:
        """新增或按 id 替换一个逻辑模型（不做自动匹配；新建请用 :meth:`create_model`）。"""
        config = self.load()
        models = [m for m in config.models if m.id != model.id]
        models.append(model)
        return self._save(_rebuild(config, models=models))

    def delete_model(self, model_id: str) -> AppConfiguration:
        """删除逻辑模型。它是默认模型时拒绝——先把默认指向别处。

        绑定随之解除，实际模型仍留在目录里（能力探测结果不丢）。
        """
        config = self.load()
        if config.default_model_id == model_id:
            raise ValueError(f"{model_id} 是默认模型，请先改默认模型再删除。")
        return self._save(_rebuild(config, models=[m for m in config.models if m.id != model_id]))

    def set_default_model(self, model_id: str) -> AppConfiguration:
        config = self.load()
        return self._save(_rebuild(config, default_model_id=model_id))

    def auto_match(self, model_id: str) -> tuple[AppConfiguration, MatchPlan]:
        """按 ID 为已有逻辑模型自动匹配实际模型（用户显式触发）。"""
        config = self.load()
        _require_model(config, model_id)
        plan = plan_for_logical(config, model_id)
        if not plan.bind:
            return config, plan
        return self._save(_rebuild(config, models=_apply_plan(config, plan))), plan

    # ---------- 绑定 ----------

    def pair_actual_model(
        self, model_id: str, endpoint_id: str, remote_model_id: str
    ) -> AppConfiguration:
        """手动配对：把目录里的一个实际模型绑到逻辑模型（同站点已有绑定则替换）。"""
        config = self.load()
        if config.actual_model(endpoint_id, remote_model_id) is None:
            raise ValueError(
                f"实际模型不存在：{endpoint_id} · {remote_model_id}（先在「实际模型」页导入）"
            )
        return self.bind_endpoint(
            model_id, ModelBinding(endpoint_id=endpoint_id, model_id=remote_model_id)
        )

    def bind_endpoint(self, model_id: str, binding: ModelBinding) -> AppConfiguration:
        """给逻辑模型增加一条绑定（同站点已有绑定视为替换）。

        能力以实际模型目录为准；目录里还没有该实际模型时按 ``binding`` 上的声明登记。
        实际模型已绑定到别的逻辑模型时拒绝——一个实际模型只能绑定一个逻辑模型。
        """
        config = self.load()
        model = _require_model(config, model_id)
        owner = config.binding_owner(binding.endpoint_id, binding.model_id)
        if owner is not None and owner != model_id:
            raise ValueError(
                f"{binding.endpoint_id} · {binding.model_id} 已绑定到逻辑模型 {owner}；"
                "一个实际模型只能绑定一个逻辑模型，请先在那边解除绑定。"
            )
        updated_model = _with_binding(model, binding)
        return self._save(_rebuild(config, models=_replace_model(config, model_id, updated_model)))

    def unbind_endpoint(self, model_id: str, endpoint_id: str) -> AppConfiguration:
        """解除一条绑定。可以解到一条不剩（逻辑模型保留，之后再配）。"""
        config = self.load()
        model = _require_model(config, model_id)
        updated_model = _without_endpoint(model, endpoint_id)
        return self._save(_rebuild(config, models=_replace_model(config, model_id, updated_model)))

    def reorder_bindings(self, model_id: str, endpoint_ids: list[str]) -> AppConfiguration:
        """保存该逻辑模型的优先级；第一条绑定即默认站点。

        必须提交当前绑定的完整排列，避免过期页面覆盖新绑定或重复/漏掉站点。
        """
        config = self.load()
        model = _require_model(config, model_id)
        by_endpoint = {binding.endpoint_id: binding for binding in model.bindings}
        if len(endpoint_ids) != len(by_endpoint) or set(endpoint_ids) != set(by_endpoint):
            raise ValueError("绑定列表已变化或排序无效，请刷新后重试")
        if endpoint_ids == [binding.endpoint_id for binding in model.bindings]:
            return config
        updated_model = _rebuild_model(
            model,
            bindings=[by_endpoint[endpoint_id] for endpoint_id in endpoint_ids],
        )
        return self._save(_rebuild(config, models=_replace_model(config, model_id, updated_model)))


def _without_endpoint(model: LogicalModel, endpoint_id: str) -> LogicalModel:
    """删除指定站点，保留其余优先级；若删除首位，下一条自然成为默认站点。"""
    remaining = [b for b in model.bindings if b.endpoint_id != endpoint_id]
    if len(remaining) == len(model.bindings):
        return model
    return _rebuild_model(model, bindings=remaining)


def _rebuild_model(
    model: LogicalModel,
    *,
    bindings: list[ModelBinding] | None = None,
) -> LogicalModel:
    """显式重建逻辑模型，让 pydantic 校验生效。"""
    return LogicalModel(
        id=model.id,
        name=model.name,
        bindings=model.bindings if bindings is None else bindings,
        context_window=model.context_window,
        max_tokens=model.max_tokens,
        supports_images=model.supports_images,
    )


def _require_model(config: AppConfiguration, model_id: str) -> LogicalModel:
    for model in config.models:
        if model.id == model_id:
            return model
    raise ValueError(f"逻辑模型不存在：{model_id}")


def _require_endpoint(config: AppConfiguration, endpoint_id: str) -> None:
    if not any(endpoint.id == endpoint_id for endpoint in config.endpoints):
        raise ValueError(f"端点不存在：{endpoint_id}")
