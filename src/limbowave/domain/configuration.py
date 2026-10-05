"""应用权威配置：站点端点 + 实际模型目录 + 逻辑模型 + 默认模型。

这是普通配置（落盘为 JSON），**绝不含密钥本体**——密钥只以 ``credential_ref`` 引用存在。
Pi 的 ``models.json`` 是从这里**派生**的运行时产物，不是权威数据源（Phase 1A 验收）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from limbowave.domain.memory import MemorySettings
from limbowave.domain.models import CAPABILITY_FIELDS, ActualModel, LogicalModel
from limbowave.domain.providers import EndpointConfig

_CAPABILITY_DEFAULTS = {name: ActualModel.model_fields[name].default for name in CAPABILITY_FIELDS}


def _fields(raw: Any) -> dict[str, Any] | None:
    """校验前的原始项（JSON 字典或已构造的模型对象）→ 字段字典（浅拷贝）。

    其他类型返回 None：原样交给字段校验去报类型错误。
    """
    if isinstance(raw, BaseModel):
        return {name: getattr(raw, name) for name in type(raw).model_fields}
    return dict(raw) if isinstance(raw, dict) else None


class CompressionSettings(BaseModel):
    """压缩自动触发的阈值（§七.1：**支持全局配置触发阈值**）。

    默认值来自计划书的建议初始值（70/80/90），可在 config.json 里调整。
    ``auto_preview``：达到预览阈值时是否**自动生成压缩预览**（否则只提示）。
    """

    model_config = ConfigDict(frozen=True)

    hint_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    preview_threshold: float = Field(default=80.0, ge=0.0, le=100.0)
    block_threshold: float = Field(default=90.0, ge=0.0, le=100.0)
    auto_preview: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> CompressionSettings:
        if not (self.hint_threshold <= self.preview_threshold <= self.block_threshold):
            raise ValueError("压缩阈值必须满足 hint ≤ preview ≤ block")
        return self


class AppConfiguration(BaseModel):
    """应用权威配置。

    实际模型目录（``actual_models``）是站点-模型能力声明的唯一来源；绑定上的能力字段
    在校验前从目录投影过去（见 :meth:`_sync_catalog`），路由直接读绑定即可。
    """

    model_config = ConfigDict(frozen=True)

    endpoints: list[EndpointConfig] = Field(default_factory=list)
    # 实际模型目录：站点上导入 / 探测 / 手动添加的远端模型（导入不会创建逻辑模型）
    actual_models: list[ActualModel] = Field(default_factory=list)
    models: list[LogicalModel] = Field(default_factory=list)
    default_model_id: str | None = None
    # 压缩自动触发的阈值（§七.1）
    compression: CompressionSettings = Field(default_factory=CompressionSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)

    @model_validator(mode="before")
    @classmethod
    def _sync_catalog(cls, data: Any) -> Any:
        """让实际模型目录与绑定一致，再做字段校验。

        - 绑定指向的实际模型不在目录里（旧版配置、直接构造）：用绑定上的能力补进目录；
        - 目录里已有：绑定上的能力以目录为准。
        """
        if not isinstance(data, dict):
            return data
        catalog: list[Any] = list(data.get("actual_models") or ())
        entries: dict[tuple[Any, Any], dict[str, Any]] = {}
        for raw in catalog:
            fields = _fields(raw)
            if fields is not None:
                entries.setdefault((fields.get("endpoint_id"), fields.get("model_id")), fields)

        models: list[Any] = []
        for raw_model in data.get("models") or ():
            model = _fields(raw_model)
            if model is None or not isinstance(model.get("bindings"), list | tuple):
                models.append(raw_model)
                continue
            bindings: list[Any] = []
            for raw_binding in model["bindings"]:
                binding = _fields(raw_binding)
                if binding is None:
                    bindings.append(raw_binding)  # 交给字段校验报错
                    continue
                key = (binding.get("endpoint_id"), binding.get("model_id"))
                if not all(isinstance(part, str) and part for part in key):
                    bindings.append(binding)
                    continue
                entry = entries.get(key)
                if entry is None:
                    entry = {"endpoint_id": key[0], "model_id": key[1]}
                    entry.update({n: binding[n] for n in CAPABILITY_FIELDS if n in binding})
                    entries[key] = entry
                    catalog.append(entry)
                binding.update({n: entry.get(n, d) for n, d in _CAPABILITY_DEFAULTS.items()})
                bindings.append(binding)
            models.append({**model, "bindings": bindings})
        return {**data, "actual_models": catalog, "models": models}

    @model_validator(mode="after")
    def _ids_unique_and_refs_valid(self) -> AppConfiguration:
        endpoint_ids = [e.id for e in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("端点 id 重复")
        model_ids = [m.id for m in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("逻辑模型 id 重复")

        endpoint_set = set(endpoint_ids)
        owners: dict[tuple[str, str], str] = {}
        for model in self.models:
            for binding in model.bindings:
                if binding.endpoint_id not in endpoint_set:
                    raise ValueError(f"模型 {model.id} 绑定了不存在的端点 {binding.endpoint_id}")
                owner = owners.setdefault(binding.key, model.id)
                if owner != model.id:
                    raise ValueError(
                        f"实际模型 {binding.endpoint_id} · {binding.model_id} 已绑定到逻辑模型"
                        f" {owner}，不能再绑定到 {model.id}（一个实际模型只能绑定一个逻辑模型）"
                    )
            if len({b.key for b in model.bindings}) != len(model.bindings):
                raise ValueError(f"模型 {model.id} 重复绑定了同一个实际模型")

        seen: set[tuple[str, str]] = set()
        for actual in self.actual_models:
            if actual.endpoint_id not in endpoint_set:
                raise ValueError(
                    f"实际模型 {actual.model_id} 属于不存在的端点 {actual.endpoint_id}"
                )
            if actual.key in seen:
                raise ValueError(f"实际模型重复：{actual.endpoint_id} · {actual.model_id}")
            seen.add(actual.key)

        if self.default_model_id is not None and self.default_model_id not in set(model_ids):
            raise ValueError(f"default_model_id 指向不存在的模型 {self.default_model_id}")
        return self

    def actual_model(self, endpoint_id: str, model_id: str) -> ActualModel | None:
        """按 (站点, 模型 ID) 查实际模型目录。"""
        return next(
            (a for a in self.actual_models if a.key == (endpoint_id, model_id)), None
        )

    def binding_owner(self, endpoint_id: str, model_id: str) -> str | None:
        """绑定了该实际模型的逻辑模型 id；未绑定为 None。"""
        for model in self.models:
            if any(b.key == (endpoint_id, model_id) for b in model.bindings):
                return model.id
        return None
