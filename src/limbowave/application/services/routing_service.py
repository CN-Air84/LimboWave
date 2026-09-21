"""路由服务：逻辑模型 → 具体端点的确定性选择。

设计约束（Phase 1A 验收 / ADR 裁决 1）：
- **确定且可解释**：同一输入永远得到同一 ``RoutingDecision``，且带 ``reason``。
- **不自动跨站点重发**：绑定缺失或端点缺失就抛 ``RoutingError``，绝不静默换站。
"""

from __future__ import annotations

from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel
from limbowave.domain.routing import RoutingDecision, RoutingError


class RoutingService:
    """按配置做确定性路由。"""

    def __init__(self, config: AppConfiguration) -> None:
        self._config = config
        self._endpoints = {e.id: e for e in config.endpoints}
        self._models = {m.id: m for m in config.models}

    def list_models(self) -> list[LogicalModel]:
        return list(self._config.models)

    def get_model(self, model_id: str) -> LogicalModel | None:
        return self._models.get(model_id)

    def route(self, model_id: str | None = None) -> RoutingDecision:
        """把逻辑模型路由到默认端点。失败抛 RoutingError。"""
        model = self._resolve_model(model_id)

        binding = model.bindings[model.default_binding]
        endpoint = self._endpoints.get(binding.endpoint_id)
        if endpoint is None:
            raise RoutingError(f"模型 {model.id} 的默认绑定指向不存在的端点 {binding.endpoint_id}")

        return RoutingDecision(
            model=model,
            binding=binding,
            endpoint=endpoint,
            reason=f"逻辑模型 {model.id} 的默认绑定（第 {model.default_binding} 个）",
        )

    def _resolve_model(self, model_id: str | None) -> LogicalModel:
        if model_id is not None:
            model = self._models.get(model_id)
            if model is None:
                raise RoutingError(f"逻辑模型不存在：{model_id}")
            return model
        # 未指定：默认模型，否则第一个
        if self._config.default_model_id is not None:
            model = self._models.get(self._config.default_model_id)
            if model is None:
                raise RoutingError(f"默认模型不存在：{self._config.default_model_id}")
            return model
        if not self._config.models:
            raise RoutingError("未配置任何逻辑模型")
        return self._config.models[0]
