"""逻辑模型 ↔ 实际模型的自动匹配（设计计划 §四.2 / Task 2.2）。

只用 :func:`normalize_model_id` 的三条确定规则（trim / 忽略大小写 / 去末尾 ``-free``），
**不做模糊相似度**。自动匹配只在明确的时机发生——新建逻辑模型、首次导入实际模型、
用户点「按 ID 自动匹配」——并且：

- 已绑定到别的逻辑模型的实际模型不动（一个实际模型只能绑定一个逻辑模型）；
- 逻辑模型在某站点已有**手动**绑定时，该站点跳过（用户的绑定结果优先）；
  自动建立的绑定只会被**更接近**的候选替换；
- 同一站点有多个同样接近的候选时不替用户挑，留给手动配对。

接近程度：完全相同 > 仅大小写 / 首尾空白不同 > 归一后相同（如多了 ``-free``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.model_normalize import normalize_model_id
from limbowave.domain.models import ActualModel, ModelBinding

EXACT = 0
CASE_ONLY = 1
NORMALIZED = 2


def match_rank(logical_id: str, model_id: str) -> int | None:
    """两个 ID 的接近程度（越小越接近）；归一后都不同返回 None。"""
    if logical_id == model_id:
        return EXACT
    if logical_id.strip().lower() == model_id.strip().lower():
        return CASE_ONLY
    target = normalize_model_id(logical_id)
    if target and target == normalize_model_id(model_id):
        return NORMALIZED
    return None


@dataclass(frozen=True, slots=True)
class MatchPlan:
    """一次自动匹配的结论（只描述，不落盘；由 SettingsService 执行）。"""

    logical_id: str
    # 将要绑定的实际模型（每个站点至多一个；会替换该站点上自动建立的绑定）
    bind: tuple[ActualModel, ...] = ()
    # ID 匹配、但已绑定到其他逻辑模型的实际模型：(实际模型, 所属逻辑模型)，不改动
    taken: tuple[tuple[ActualModel, str], ...] = ()
    # 同一站点有多个同样接近的候选：(站点 id, 候选模型 ID)，留给用户手动配对
    ambiguous: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _replaceable(binding: ModelBinding | None, logical_id: str, rank: int) -> bool:
    """该站点能否（再）自动绑定：没有绑定，或自动建立的绑定不如新候选接近。"""
    if binding is None:
        return True
    if not binding.auto_matched:
        return False
    current = match_rank(logical_id, binding.model_id)
    return current is None or rank < current


def plan_for_logical(config: AppConfiguration, logical_id: str) -> MatchPlan:
    """逻辑模型 ``logical_id`` 在目录里能自动绑定哪些实际模型（逻辑模型可以尚未保存）。"""
    model = next((m for m in config.models if m.id == logical_id), None)
    by_endpoint = {b.endpoint_id: b for b in model.bindings} if model else {}
    owners = {b.key: m.id for m in config.models for b in m.bindings}
    candidates: dict[str, list[tuple[int, ActualModel]]] = {}
    taken: list[tuple[ActualModel, str]] = []
    for actual in config.actual_models:
        rank = match_rank(logical_id, actual.model_id)
        if rank is None:
            continue
        owner = owners.get(actual.key)
        if owner is not None:
            if owner != logical_id:
                taken.append((actual, owner))
            continue
        if _replaceable(by_endpoint.get(actual.endpoint_id), logical_id, rank):
            candidates.setdefault(actual.endpoint_id, []).append((rank, actual))

    bind: list[ActualModel] = []
    ambiguous: list[tuple[str, tuple[str, ...]]] = []
    for endpoint_id, ranked in candidates.items():
        best = min(rank for rank, _ in ranked)
        top = [actual for rank, actual in ranked if rank == best]
        if len(top) == 1:
            bind.append(top[0])
        else:
            ambiguous.append((endpoint_id, tuple(actual.model_id for actual in top)))
    # 绑定顺序即备用顺序：站点优先级高的在前，其次按站点列表顺序
    order = {e.id: (-e.priority, index) for index, e in enumerate(config.endpoints)}
    bind.sort(key=lambda actual: order.get(actual.endpoint_id, (0, len(order))))
    return MatchPlan(logical_id, tuple(bind), tuple(taken), tuple(ambiguous))


def logical_for_actual(config: AppConfiguration, actual: ActualModel) -> str | None:
    """首次导入的实际模型应自动归入哪个逻辑模型；没有唯一最接近的就返回 None。"""
    if config.binding_owner(actual.endpoint_id, actual.model_id) is not None:
        return None
    ranked: list[tuple[int, str]] = []
    for model in config.models:
        rank = match_rank(model.id, actual.model_id)
        if rank is None:
            continue
        current = next((b for b in model.bindings if b.endpoint_id == actual.endpoint_id), None)
        if _replaceable(current, model.id, rank):
            ranked.append((rank, model.id))
    if not ranked:
        return None
    best = min(rank for rank, _ in ranked)
    top = [model_id for rank, model_id in ranked if rank == best]
    return top[0] if len(top) == 1 else None
