"""逻辑模型与端点绑定的领域模型。

「逻辑模型」是应用侧的稳定概念（如 `deepseek-chat`），可绑定到一个或多个站点端点；
「模型 ID」是该端点上真正的远端模型名。二者分离（设计计划 §四.1）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBinding(BaseModel):
    """逻辑模型到一个站点端点的绑定。"""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)  # 该端点上的实际模型 ID


class LogicalModel(BaseModel):
    """逻辑模型：应用侧的稳定会话目标。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    bindings: list[ModelBinding] = Field(min_length=1)
    # 默认绑定的索引（bindings 中的位置）
    default_binding: int = 0
    # 模型能力元数据（可选；缺省时派生 models.json 用 Pi 默认值）
    context_window: int | None = None
    max_tokens: int | None = None
    supports_images: bool = False
