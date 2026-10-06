"""Qt-free history projection shared by the GUI and conversation worker."""

from typing import NamedTuple

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.tool_step_payload import prepare_tool_step
from limbowave.domain.compaction import CompressionContext, CompressionStatus
from limbowave.domain.conversation import (
    AssistantMessageSegment,
    Message,
    MessageRole,
    MessageStatus,
)
from limbowave.domain.run import RunStatus
from limbowave.domain.tool_step import ToolStep


class HistoryEntry(NamedTuple):
    """历史载荷：一条消息的展示数据。

    ``run_id`` 相同且相邻的助手条目渲染进**同一张卡片**（一轮 Agent run 一卡，
    卡内按消息分段）。``run_id`` / ``tool_steps`` 有默认值：不带这两个字段的
    旧 4 元组载荷仍可用，此时每条消息一张卡，行为与合并前一致。
    ``compressions`` 是此消息之后的压缩分隔线，不计入消息数量或模型上下文。
    """

    role: str
    content: str
    thinking: str
    message_id: str | None
    run_id: str | None = None
    tool_steps: tuple[ToolStep, ...] = ()
    retry_available: bool = False
    can_fork: bool = True
    segments: tuple[AssistantMessageSegment, ...] = ()
    # Presentation-only boundaries: original messages and model context stay unchanged.
    compressions: tuple[CompressionContext, ...] = ()



def history_payload(
    messages: list[Message], *, uow_factory: UnitOfWorkFactory | None = None,
    branch_id: str | None = None,
) -> list[HistoryEntry]:
    """历史消息 → 视图载荷：同轮合卡，并恢复失败/停止后的重试入口。

    无正文时没有助手消息，须从 Run 状态恢复用户消息的重试入口。
    只在最近一条用户消息上提供重试，与实时视图开始新一轮时的清理行为一致。
    压缩记录必须显式指定当前 branch_id；继承的消息归属不代表当前分支。
    """
    compressions: dict[str, list[CompressionContext]] = {}
    if branch_id is not None and uow_factory is not None:
        with uow_factory() as uow:
            active = uow.compressions.get_active(branch_id)
            versions = sorted(uow.compressions.list_for_branch(branch_id),
                              key=lambda version: (version.created_at, version.id))
        message_ids = {message.id for message in messages}
        for version in versions:
            if version.status is not CompressionStatus.ACCEPTED or not version.input_message_ids:
                continue
            boundary = version.input_message_ids[-1]
            if boundary in message_ids:
                compressions.setdefault(boundary, []).append(
                    CompressionContext(
                        version, is_active=active is not None and active.id == version.id
                    )
                )

    retryable_runs = {
        m.run_id
        for m in messages
        if m.status in {MessageStatus.PARTIAL, MessageStatus.FAILED} and m.run_id is not None
    }
    retryable_users: set[str] = set()
    retry_sources: dict[str, str] = {}
    replaced_runs: set[str] = set()
    if messages and uow_factory is not None:
        with uow_factory() as uow:
            runs = uow.runs.list_for_conversation(messages[0].conversation_id)
            runs_by_user = {run.user_message_id: run for run in runs}
            for run in runs:
                if run.status in {RunStatus.FAILED, RunStatus.ABORTED, RunStatus.INTERRUPTED}:
                    retryable_runs.add(run.id)
                    retryable_users.add(run.user_message_id)
                if run.retry_of_message_id is not None:
                    retry_sources[run.user_message_id] = run.retry_of_message_id
            # 重生成/编辑在重试用户消息之前截断，来源消息虽不在新路径里，
            # 它替换过的旧尝试仍不可复活。沿分支祖先恢复这个展示边界，
            # 不改原始消息、运行审计或旧分支，也不按相同文本去重。
            seen_branches: set[str] = set()
            cursor = branch_id
            while cursor is not None and cursor not in seen_branches:
                seen_branches.add(cursor)
                branch = uow.branches.get(cursor)
                if branch is None:
                    break
                if not branch.include_fork_message:
                    source = branch.forked_from_message_id
                    seen_sources: set[str] = set()
                    while source in retry_sources and source not in seen_sources:
                        seen_sources.add(source)
                        source = retry_sources[source]
                        prior = runs_by_user.get(source)
                        if prior is not None:
                            replaced_runs.add(prior.id)
                cursor = branch.parent_branch_id
    # 只替换当前分支路径中紧邻的来源轮；不按文本去重，不吞掉分叉后的独立消息。
    previous_user: Message | None = None
    for message in messages:
        if message.role is not MessageRole.USER:
            continue
        if (
            previous_user is not None
            and previous_user.run_id is not None
            and retry_sources.get(message.id) == previous_user.id
        ):
            replaced_runs.add(previous_user.run_id)
        previous_user = message
    messages = [m for m in messages if m.run_id not in replaced_runs]
    last_user_id = next(
        (m.id for m in reversed(messages) if m.role is MessageRole.USER), None
    )
    return [
        HistoryEntry(
            m.role.value,
            m.content,
            m.thinking,
            m.id,
            m.run_id,
            tuple(prepare_tool_step(step) for step in m.tool_steps),
            retry_available=(
                m.id == last_user_id
                and (m.id in retryable_users or m.run_id in retryable_runs)
            ),
            can_fork=m.status is MessageStatus.COMPLETE and m.run_id not in retryable_runs,
            segments=m.segments,
            compressions=tuple(compressions.get(m.id, ())),
        )
        for m in messages
    ]
