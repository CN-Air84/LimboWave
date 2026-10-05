"""逻辑模型、实际模型与绑定的领域模型。

- 「实际模型」是某个站点上真实可调用的远端模型（站点 + 模型 ID），来自站点模型清单
  导入、能力探测或手动输入，收在实际模型目录里；
- 「逻辑模型」是**用户手动建立**的分类（如 `deepseek-chat`），应用侧的稳定会话目标；
- 「绑定」把实际模型归到逻辑模型下：按 ID 归一规则自动匹配，或由用户手动配对。
  **一个实际模型最多绑定一个逻辑模型**（设计计划 §四.1 / §四.2）。

导入实际模型**不会**创建逻辑模型——逻辑模型只由用户建立。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ModelCapability = Literal["supports_streaming", "supports_thinking", "supports_tools"]

# 站点-模型级能力声明的字段名。权威值在实际模型目录（ActualModel），
# 绑定上的同名字段只是投影（见 ModelBinding）。
CAPABILITY_FIELDS = (
    "supports_streaming",
    "default_thinking_level",
    "thinking_level_locked",
    "available_thinking_levels",
    "user_thinking_levels",
    "supports_thinking",
    "supports_tools",
)


class ActualModel(BaseModel):
    """实际模型：某个站点上的一个远端模型，及其能力声明。

    **能力声明属于这里**：思考强度、工具支持这些是「某个站点上的某个远端模型」的属性——
    同一个逻辑模型绑到官方站点和中转站，能力可能完全不同（中转站常常不支持 reasoning）。
    """

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)  # 该端点上的实际模型 ID
    # 站点模型清单给出的显示名；空 = 直接显示模型 ID
    name: str = ""
    # None = 尚未确认；能力可由检测或用户声明，旧配置不会被推断为支持。
    supports_streaming: bool | None = None
    # 思考强度默认值（§二.4 模型级覆盖）：路由到该模型时应用。None = 不设置
    default_thinking_level: str | None = None
    # 仅显式配置或可靠元数据可锁定；模型名称不能作为固定等级的证据。
    thinking_level_locked: bool = False
    # 逐级探测实际验证的等级；空值表示尚无逐级探测结果。
    available_thinking_levels: tuple[str, ...] = ()
    # 用户在真实请求成功后明确确认的等级；与自动探测来源分开保存。
    user_thinking_levels: tuple[str, ...] = ()
    # None = 尚未确认；True/False = 检测结果或用户声明
    supports_thinking: bool | None = None
    # 工具能力声明（§二.4 模型级覆盖）：由检测或用户设置，供工具模式判断使用
    supports_tools: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """目录里的唯一键：(站点 id, 模型 ID)。"""
        return (self.endpoint_id, self.model_id)

    @property
    def display_name(self) -> str:
        return self.name or self.model_id


class ModelBinding(BaseModel):
    """逻辑模型对一个实际模型（站点 + 远端模型 ID）的绑定。

    能力字段是实际模型目录条目的**投影**：``AppConfiguration`` 校验时从目录同步
    （目录里还没有该实际模型时反过来用这里的值补进目录——旧版配置即由此迁移），
    路由与运行时直接读它们即可。它们不单独落盘（``exclude``），config.json 里
    能力只在 ``actual_models`` 出现一次。
    """

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)  # 该端点上的实际模型 ID
    # 由 ID 自动匹配建立（False = 用户手动配对 / 旧版配置）。自动匹配只会用更接近的
    # 候选替换自动建立的绑定，**从不改动手动配对**——用户的绑定结果优先（§四.2）。
    auto_matched: bool = False
    # ---- 以下为实际模型能力的投影 ----
    supports_streaming: bool | None = Field(default=None, exclude=True)
    default_thinking_level: str | None = Field(default=None, exclude=True)
    thinking_level_locked: bool = Field(default=False, exclude=True)
    available_thinking_levels: tuple[str, ...] = Field(default=(), exclude=True)
    user_thinking_levels: tuple[str, ...] = Field(default=(), exclude=True)
    supports_thinking: bool | None = Field(default=None, exclude=True)
    supports_tools: bool = Field(default=False, exclude=True)

    @property
    def key(self) -> tuple[str, str]:
        """指向的实际模型：(站点 id, 模型 ID)。"""
        return (self.endpoint_id, self.model_id)


class LogicalModel(BaseModel):
    """逻辑模型：用户手动建立的分类，也是应用侧的稳定会话目标。

    可以先建后绑：``bindings`` 为空表示还没有绑定实际模型（路由会如实报错）。
    同一站点至多一条绑定（会话级站点覆盖与备用站点都按站点区分）。
    bindings 按优先级从高到低排列，第一条就是默认站点。旧 default_binding 字段忽略。
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    bindings: list[ModelBinding] = Field(default_factory=list)
    # 模型能力元数据（可选；缺省时派生 models.json 用 Pi 默认值）
    context_window: int | None = None
    max_tokens: int | None = None
    supports_images: bool = False
