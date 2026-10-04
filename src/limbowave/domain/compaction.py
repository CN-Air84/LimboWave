"""上下文压缩的领域模型（Phase 5 / 设计计划 §七）。

两个核心概念：

- **白名单消息**（§7.3）：用户标记的重要消息。压缩时**默认保留原文**，
  压缩模型被明确指示不得压缩它们。白名单语义不得被静默破坏——
  实在装不下时提示用户处理，不悄悄删掉。
- **压缩版本**（§7.4）：每次压缩产出一条版本记录。原始消息**永久保留**
  在数据库里，压缩只是「拟发送上下文」的一层。每个版本保存：
  输入消息范围、原始/压缩后 token 估算、压缩模型与站点、提示词版本、
  生成结果、用户编辑后的结果、启用状态、创建时间与状态。

状态机（§7.1）：draft → previewed → accepted / rejected。
retry 产出新版本（不覆盖旧版本）；rollback 把启用标记移回旧版本。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class CompressionStatus(enum.StrEnum):
    """压缩版本的状态。"""

    DRAFT = "draft"  # 已生成，未预览
    PREVIEWED = "previewed"  # 已展示给用户
    ACCEPTED = "accepted"  # 用户接受（成为可启用版本）
    REJECTED = "rejected"  # 用户拒绝
    FAILED = "failed"  # 生成失败（不修改有效上下文）


@dataclass(frozen=True, slots=True)
class CompressionVersion:
    """一次压缩的版本记录。不可变；编辑通过 ``with_edited_summary`` 派生。"""

    id: str
    conversation_id: str
    branch_id: str
    created_at: datetime
    status: CompressionStatus
    # 输入范围与估算
    input_message_ids: tuple[str, ...] = ()
    tokens_before: int = 0
    tokens_after: int = 0
    # 压缩模型与站点
    compression_model_id: str = ""
    compression_endpoint_id: str = ""
    # 提示词版本（压缩提示词迭代时递增，便于回溯是哪版提示词产出的）
    prompt_version: int = 1
    # 结果
    generated_summary: str = ""  # 模型生成的原始摘要
    edited_summary: str | None = None  # 用户编辑后的摘要（None = 未编辑）
    # 白名单快照：本次压缩时哪些消息被标记保留原文
    whitelist_message_ids: tuple[str, ...] = ()
    error: str | None = None

    @property
    def effective_summary(self) -> str:
        """当前生效的摘要：用户编辑过用编辑版，否则用生成版。"""
        return self.edited_summary if self.edited_summary is not None else self.generated_summary

    def with_edited_summary(self, edited: str) -> CompressionVersion:
        """派生一个用户编辑后的版本（不改原对象）。"""
        from dataclasses import replace

        return replace(self, edited_summary=edited)


@dataclass(frozen=True, slots=True)
class CompressionContext:
    """一次压缩的完整上下文（版本 + 是否当前启用）。

    「当前启用版本」不放在版本对象里——同一时刻一个分支只能有一个启用版本，
    由存储层保证唯一性，避免版本对象间状态不一致。
    """

    version: CompressionVersion
    is_active: bool = False


# ---------- 压缩提示词（§7.2 的组成，版本化） ----------

# 提示词版本号：改提示词模板时递增，旧版本产出的摘要在审计里可区分。
COMPRESSION_PROMPT_VERSION = 1

_COMPRESSION_TEMPLATE = """你是上下文压缩器。
把一段对话历史压缩成一份摘要，供后续对话作为上下文使用。

## 必须保留
- 当前会话目标：{goal}
- 已完成与未完成任务：{tasks}
- 文件读取引用：{file_refs}

## 白名单消息（原文保留，不得压缩、不得改写、不得遗漏）
{whitelist}

## 铁律
- 禁止编造缺失的信息。摘要里没有的事实就是没有，不得脑补。
- 白名单消息必须**原文**出现在摘要的「白名单原文」一节，一个字都不能改。
- 其余消息压缩为要点，保留关键决定、约束、偏好与未决问题。

## 待压缩消息
{messages}

## 输出格式
### 白名单原文
（逐条原文，或无）
### 会话目标
### 进展与任务
### 关键决定与约束
### 未决问题与下一步
"""


def build_compression_prompt(
    *,
    goal: str,
    tasks: str,
    file_refs: str,
    whitelist: list[str],
    messages: list[str],
) -> str:
    """组装压缩提示词（§7.2 的全部组成部分）。纯函数，可测。"""
    return _COMPRESSION_TEMPLATE.format(
        goal=goal or "（未明确）",
        tasks=tasks or "（无）",
        file_refs=file_refs or "（无）",
        whitelist="\n\n".join(whitelist) if whitelist else "（无）",
        messages="\n\n".join(messages),
    )
