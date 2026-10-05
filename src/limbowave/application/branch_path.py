"""分支路径：把「分支」还原成它实际代表的那段对话。

存储模型（Task 3.2）：每条消息只落在**产生它的分支**上。分叉出的新分支只记录
分叉之后的新消息，分叉点之前的内容仍归父分支所有。因此：

- 分支的完整对话 = 父分支截到分叉边界的前缀 + 本分支自己的消息。
  Fork 保留起点助手回复；编辑与重新生成从用户消息之前分叉，替换而非保留起点。
- 只用 ``messages.list_for_branch`` 渲染分支，会丢掉整段前缀；而拿"最新分支"代替
  "指定分支"，会让切回旧分支时看到新分支的内容——两者都是这里要堵住的错误。

运行时侧同理：分支叶子 = 本分支最近一轮运行镜像的链尾，不是"最后一条能按文本对上
的消息"（带工具调用的一轮里，助手终答未必能按文本匹配回应用消息）。
"""

from __future__ import annotations

from limbowave.application.repositories import UnitOfWork
from limbowave.domain.conversation import Branch, Message
from limbowave.domain.run import RunRecord
from limbowave.domain.runtime_mirror import RuntimeEntryMirror

# 分支链的深度上限：防御损坏数据里的环
_MAX_DEPTH = 256


def branch_messages(uow: UnitOfWork, branch_id: str) -> list[Message]:
    """分支的完整对话（前缀 + 本分支消息），按时间顺序。分支不存在返回空。"""
    chain: list[Branch] = []
    seen: set[str] = set()
    cursor: str | None = branch_id
    while cursor is not None and cursor not in seen and len(chain) < _MAX_DEPTH:
        branch = uow.branches.get(cursor)
        if branch is None:
            break
        seen.add(cursor)
        chain.append(branch)
        cursor = branch.parent_branch_id
    if not chain:
        return []

    # 从根往下拼：每一层把已拼好的上层对话截到本层的分叉点，再接上本层消息
    messages: list[Message] = []
    for child in reversed(chain):
        if child.forked_from_message_id is not None:
            messages = _cut_at_fork(messages, child)
        messages.extend(uow.messages.list_for_branch(child.id))
    return messages


def _cut_at_fork(messages: list[Message], child: Branch) -> list[Message]:
    for index, message in enumerate(messages):
        if message.id == child.forked_from_message_id:
            return messages[:index + int(child.include_fork_message)]
    # 分叉消息找不到（数据不完整）：退到按分支创建时间截断
    return [m for m in messages if m.created_at < child.created_at]


def current_branch(uow: UnitOfWork, conversation_id: str) -> Branch | None:
    """会话的当前分支：最近有活动（发过消息或刚分叉出来）的那条。

    不能用"最晚创建"：切回旧分支继续聊之后，旧分支才是用户正在用的那条。
    """
    branches = uow.branches.list_for_conversation(conversation_id)
    if not branches:
        return None

    def _activity(branch: Branch) -> tuple[object, ...]:
        own = uow.messages.list_for_branch(branch.id)
        last = max([branch.created_at, *(m.created_at for m in own)])
        return (last, branch.created_at, branch.id)

    return max(branches, key=_activity)


def branch_leaf_entry(uow: UnitOfWork, branch_id: str) -> str | None:
    """分支叶子的运行时条目 ID（恢复该分支上下文用）。

    取本分支最近一轮有镜像的运行，其镜像链的尾巴就是叶子。本分支还没有运行记录
    时退到分叉边界：Fork 用起点回复的整轮链尾，编辑/重生成用用户条目的父条目。
    """
    branch = uow.branches.get(branch_id)
    if branch is None:
        return None
    return branch_leaf_entry_from_records(
        branch,
        uow.runs.list_for_conversation(branch.conversation_id),
        uow.runtime.list_for_conversation(branch.conversation_id),
    )


def branch_leaf_entry_from_records(
    branch: Branch,
    runs: list[RunRecord],
    mirrors: list[RuntimeEntryMirror],
) -> str | None:
    """从已读取记录定位叶子，供切换热路径复用一次解密结果。"""
    branch_runs = sorted(
        (run for run in runs if run.branch_id == branch.id),
        key=lambda run: (run.created_at, run.id),
        reverse=True,
    )
    mirrors_by_run: dict[str, list[RuntimeEntryMirror]] = {}
    for mirror in mirrors:
        if mirror.run_id is not None:
            mirrors_by_run.setdefault(mirror.run_id, []).append(mirror)
    for run in branch_runs:
        leaf = _mirror_leaf(mirrors_by_run.get(run.id, []))
        if leaf is not None:
            return leaf

    if branch.forked_from_message_id is not None:
        if branch.include_fork_message:
            # 一轮可包含多段助手消息和工具结果；应用回复不一定能按文本关联镜像。
            for run in runs:
                if run.assistant_message_id == branch.forked_from_message_id:
                    leaf = _mirror_leaf(mirrors_by_run.get(run.id, []))
                    if leaf is not None:
                        return leaf
        for mirror in mirrors:
            if mirror.message_id == branch.forked_from_message_id:
                return mirror.entry_id if branch.include_fork_message else mirror.parent_entry_id
    return None


def _mirror_leaf(mirrors: list[RuntimeEntryMirror]) -> str | None:
    ordered = sorted(mirrors, key=lambda mirror: (mirror.captured_at, mirror.id))
    parents = {mirror.parent_entry_id for mirror in ordered if mirror.parent_entry_id}
    tips = [mirror for mirror in ordered if mirror.entry_id not in parents]
    return tips[-1].entry_id if tips else None
