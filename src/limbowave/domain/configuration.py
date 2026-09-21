"""应用权威配置：站点端点 + 逻辑模型 + 默认模型。

这是普通配置（落盘为 JSON），**绝不含密钥本体**——密钥只以 ``credential_ref`` 引用存在。
Pi 的 ``models.json`` 是从这里**派生**的运行时产物，不是权威数据源（Phase 1A 验收）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from limbowave.domain.models import LogicalModel
from limbowave.domain.providers import EndpointConfig


class AppConfiguration(BaseModel):
    """应用权威配置。"""

    model_config = ConfigDict(frozen=True)

    endpoints: list[EndpointConfig] = Field(default_factory=list)
    models: list[LogicalModel] = Field(default_factory=list)
    default_model_id: str | None = None

    @model_validator(mode="after")
    def _ids_unique_and_refs_valid(self) -> AppConfiguration:
        endpoint_ids = [e.id for e in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("端点 id 重复")
        model_ids = [m.id for m in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("逻辑模型 id 重复")

        endpoint_set = set(endpoint_ids)
        for model in self.models:
            for binding in model.bindings:
                if binding.endpoint_id not in endpoint_set:
                    raise ValueError(f"模型 {model.id} 绑定了不存在的端点 {binding.endpoint_id}")
            if not (0 <= model.default_binding < len(model.bindings)):
                raise ValueError(f"模型 {model.id} 的 default_binding 越界")

        if self.default_model_id is not None and self.default_model_id not in set(model_ids):
            raise ValueError(f"default_model_id 指向不存在的模型 {self.default_model_id}")
        return self
