"""路由决策：逻辑模型 → 具体端点的确定性选择。

设计约束（设计计划 §四.3 / Phase 1A 验收）：
- 路由**确定且可解释**：每次决策都带 ``reason``。
- **不自动跨站点重发**：端点失败时如实暴露错误，是否切换由用户决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig


class RoutingError(Exception):
    """路由失败（模型不存在、绑定缺失、端点缺失等）。"""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """一次路由决策的结果。"""

    model: LogicalModel
    binding: ModelBinding
    endpoint: EndpointConfig
    reason: str

    @property
    def provider_key(self) -> str:
        """Pi ``models.json`` 里的 provider 键（用端点 id）。"""
        return self.endpoint.id

    @property
    def model_id(self) -> str:
        """该端点上的实际远端模型 ID。"""
        return self.binding.model_id
