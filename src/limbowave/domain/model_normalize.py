"""模型 ID 归一（设计计划 §四.2 / Task 2.2）。

归一只做三件**确定**的事：

1. ``trim``——去首尾空白；
2. ``lowercase``——忽略大小写；
3. 去掉末尾的 ``-free`` 后缀。

**明确不做**：模糊相似度匹配、擅自合并。归一结果只用于**建议与初始归类**（新建逻辑
模型、首次导入实际模型时按 ID 自动配对，见 :mod:`limbowave.domain.model_matching`），
用户的手动绑定永远具有最高优先级。
"""

from __future__ import annotations

_FREE_SUFFIX = "-free"


def normalize_model_id(model_id: str) -> str:
    """归一一个远端模型 ID。纯函数，三次确定性变换，仅此而已。"""
    normalized = model_id.strip().lower()
    if normalized.endswith(_FREE_SUFFIX):
        normalized = normalized[: -len(_FREE_SUFFIX)]
    return normalized


def suggest_logical_models(model_id: str, existing: list[str]) -> list[str]:
    """在既有模型 ID 里找出归一后与 ``model_id`` 相同的项（建议，不是合并）。

    返回的是**候选列表**——可能为空，可能有多项（说明既有数据里已存在
    归一冲突，同样如实返回，由用户决定）。
    """
    target = normalize_model_id(model_id)
    if not target:
        return []
    return [item for item in existing if normalize_model_id(item) == target]
