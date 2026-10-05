"""上下文压缩服务（Phase 5 / 设计计划 §七）。

职责：
- **估算**（Task 5.1）：从内核读 contextUsage，展示为「估算」。阈值触发分级：
  70% 提示、80% 生成预览、90% 阻止发送。
- **生成压缩请求**（Task 5.2）：专用压缩模型 + 白名单原文保留 + 文件引用 +
  当前目标 + 未完成任务 + 禁止编造规则。提示词版本化。
- **预览与版本**（Task 5.3）：每次压缩产出一个版本记录。查看/编辑/重试/接受/
  回退任意版本。**失败不修改有效上下文**——失败版本落 ``failed`` 状态，
  当前启用版本不变。

生产入口通过 ``generate_isolated`` 创建临时模型内核，不触碰主会话。
接受/回退由 RunCoordinator 先恢复带原生压缩覆盖层的运行时快照，再提交版本。
白名单与版本化仍由应用管理；原始历史永久保留。
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from limbowave.application.background import run_blocking
from limbowave.application.branch_path import branch_messages
from limbowave.application.kernel import AgentKernel, ContextUsage, KernelEvent
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.compaction import (
    COMPRESSION_PROMPT_VERSION,
    CompressionStatus,
    CompressionVersion,
    build_compression_prompt,
)
from limbowave.domain.configuration import CompressionSettings
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.redaction import redact_text

# ---------- 阈值（§七.1 的默认值，可用性测试后可调） ----------


class ThresholdAction(enum.StrEnum):
    """占用达到阈值时建议的动作。"""

    NONE = "none"  # <70%
    HINT = "hint"  # ≥70%：提示即将需要压缩
    PREVIEW = "preview"  # ≥80%：生成压缩预览
    BLOCK = "block"  # ≥90%：阻止可能超限的发送


DEFAULT_HINT_THRESHOLD = 70.0
DEFAULT_PREVIEW_THRESHOLD = 80.0
DEFAULT_BLOCK_THRESHOLD = 90.0


def threshold_action(
    percent: float,
    *,
    hint: float = DEFAULT_HINT_THRESHOLD,
    preview: float = DEFAULT_PREVIEW_THRESHOLD,
    block: float = DEFAULT_BLOCK_THRESHOLD,
) -> ThresholdAction:
    """占用百分比 → 建议动作。纯函数，可测。"""
    if percent >= block:
        return ThresholdAction.BLOCK
    if percent >= preview:
        return ThresholdAction.PREVIEW
    if percent >= hint:
        return ThresholdAction.HINT
    return ThresholdAction.NONE


# ---------- 服务 ----------


@dataclass(frozen=True, slots=True)
class UsageReport:
    """一次上下文占用报告。percent 是**估算**。"""

    usage: ContextUsage | None  # None = 内核不支持估算
    action: ThresholdAction
    display: str  # 展示文本，如 "约 42%（估算）"


class CompressionService:
    """压缩的版本管理与生成编排。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    # ---------- 估算（Task 5.1） ----------

    async def estimate(
        self,
        kernel: AgentKernel | None,
        *,
        settings: CompressionSettings | None = None,
    ) -> UsageReport:
        """读内核的上下文占用，给出一个**估算**报告。阈值可全局配置（§七.1）。"""
        if kernel is None:
            return UsageReport(usage=None, action=ThresholdAction.NONE, display="未知")
        usage = await kernel.get_context_usage()
        if usage is None:
            return UsageReport(usage=None, action=ThresholdAction.NONE, display="未知")
        active = settings or CompressionSettings()
        action = threshold_action(
            usage.percent,
            hint=active.hint_threshold,
            preview=active.preview_threshold,
            block=active.block_threshold,
        )
        return UsageReport(
            usage=usage,
            action=action,
            display=(
                f"约 {usage.percent:.0f}%（{usage.tokens}/{usage.context_window} tokens，估算）"
            ),
        )

    # ---------- 白名单（§7.3） ----------

    def toggle_whitelist(self, message_id: str) -> bool:
        """切换一条消息的白名单标记。返回新状态。"""
        with self._uow_factory() as uow:
            message = uow.messages.get(message_id)
            if message is None:
                return False
            from dataclasses import replace

            updated = replace(message, is_whitelisted=not message.is_whitelisted)
            uow.messages.update(updated)
            uow.commit()
            return updated.is_whitelisted

    def whitelisted_messages(self, branch_id: str) -> list[Message]:
        """当前分支的白名单消息（压缩时保留原文）。"""
        with self._uow_factory() as uow:
            return [m for m in branch_messages(uow, branch_id) if m.is_whitelisted]

    # ---------- 版本管理（Task 5.3） ----------

    def create_version(
        self,
        conversation_id: str,
        branch_id: str,
        *,
        tokens_before: int,
        compression_model_id: str,
        compression_endpoint_id: str,
    ) -> CompressionVersion:
        """创建一个新的压缩版本（draft）。输入范围 = 当前分支完整对话（含继承前缀）。"""
        with self._uow_factory() as uow:
            messages = branch_messages(uow, branch_id)
            whitelist = tuple(m.id for m in messages if m.is_whitelisted)
            version = CompressionVersion(
                id=f"cmp_{uuid4().hex[:16]}",
                conversation_id=conversation_id,
                branch_id=branch_id,
                created_at=datetime.now(UTC),
                status=CompressionStatus.DRAFT,
                input_message_ids=tuple(m.id for m in messages),
                tokens_before=tokens_before,
                compression_model_id=compression_model_id,
                compression_endpoint_id=compression_endpoint_id,
                prompt_version=COMPRESSION_PROMPT_VERSION,
                whitelist_message_ids=whitelist,
            )
            uow.compressions.add(version)
            uow.commit()
            return version

    def build_prompt(
        self, branch_id: str, *, goal: str = "", tasks: str = "",
        message_ids: tuple[str, ...] | None = None,
    ) -> str:
        """组装压缩提示词（Task 5.2 的全部组成部分）。

        白名单消息原文注入，文件引用与目标/任务如实呈现。
        """
        with self._uow_factory() as uow:
            messages = branch_messages(uow, branch_id)
        if message_ids is not None:
            by_id = {m.id: m for m in messages}
            messages = [by_id[mid] for mid in message_ids]
        whitelist_texts = [m.content for m in messages if m.is_whitelisted]
        file_refs = sorted(
            {
                f"[{m.id}]"
                for m in messages
                if m.role is MessageRole.USER  # 文件引用跟随用户消息
            }
        )
        compressible = [
            f"{'用户' if m.role is MessageRole.USER else '助手'}: {m.content}"
            for m in messages
            if not m.is_whitelisted  # 白名单不重复出现在待压缩区
        ]
        return build_compression_prompt(
            goal=goal,
            tasks=tasks,
            file_refs=", ".join(file_refs) if file_refs else "",
            whitelist=whitelist_texts,
            messages=compressible,
        )

    async def generate_isolated(
        self, version_id: str, source: AgentKernel, *, timeout: float = 120.0,
        on_event: Callable[[KernelEvent], None] | None = None,
    ) -> bool:
        """Generate a preview in a disposable kernel; never fall back to the live chat."""
        import asyncio
        import contextlib

        isolated: AgentKernel | None = None
        unsubscribe: Callable[[], None] | None = None
        try:
            async with asyncio.timeout(timeout):
                isolated = source.create_isolated()
                if isolated is None or isolated is source:
                    isolated = None
                    raise RuntimeError('当前内核不支持隔离压缩，未向主会话发送请求')
                if on_event is not None:
                    unsubscribe = isolated.subscribe(on_event)
                state = await source.get_state()
                await isolated.start()
                if state.provider and state.model_id:
                    await isolated.set_model(state.provider, state.model_id)
                await isolated.set_thinking_level(state.thinking_level)
                return await self.generate(version_id, isolated, timeout=timeout)
        except asyncio.CancelledError:
            await run_blocking(self.record_failure, version_id, '压缩已取消')
            raise
        except Exception as exc:
            error = '压缩生成超时' if isinstance(exc, TimeoutError) else redact_text(str(exc))
            await run_blocking(self.record_failure, version_id, error)
            return False
        finally:
            if unsubscribe is not None:
                unsubscribe()
            if isolated is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(isolated.shutdown(), timeout=10.0)

    async def generate(
        self,
        version_id: str,
        kernel: AgentKernel,
        *,
        goal: str = "",
        tasks: str = "",
        timeout: float = 120.0,
    ) -> bool:
        """用**压缩专用内核**生成摘要（Task 5.2 的执行）。

        ``kernel`` 应由调用方按「压缩逻辑模型」装配（可以是与主对话不同的模型/站点）。
        流程：组装提示词 → 发一条 prompt → 收集助手文本 → 落结果；
        任何失败都落 ``failed`` 并**不修改有效上下文**。

        返回是否成功。
        """
        import asyncio

        version = await run_blocking(self.get, version_id)
        if version is None:
            return False

        prompt = await run_blocking(
            self.build_prompt, version.branch_id, goal=goal, tasks=tasks,
            message_ids=version.input_message_ids,
        )
        chunks: list[str] = []
        settled = asyncio.Event()
        runtime_error: list[str] = []

        def _on_event(event: object) -> None:
            kind = getattr(event, "kind", "")
            payload = getattr(event, "payload", {}) or {}
            if kind == "message.end":
                message = payload.get("message") or {}
                if message.get("role") == "assistant":
                    if message.get("stopReason") in {"error", "aborted"}:
                        chunks.clear()
                        return
                    content = message.get("content")
                    if isinstance(content, list):
                        chunks.clear()
                        chunks.append(
                            "".join(
                                str(b.get("text", ""))
                                for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        )
            elif kind == "runtime.exited":
                runtime_error.append("压缩内核提前退出")
                settled.set()
            elif kind == "run.settled":
                settled.set()

        unsubscribe = kernel.subscribe(_on_event)
        try:
            await kernel.send_message(prompt)
            await asyncio.wait_for(settled.wait(), timeout=timeout)
            if runtime_error:
                raise RuntimeError(runtime_error[0])
        except Exception as exc:
            await run_blocking(
                self.record_failure, version_id,
                "压缩生成超时" if isinstance(exc, TimeoutError) else redact_text(str(exc)),
            )
            return False
        finally:
            unsubscribe()

        summary = "".join(chunks).strip()
        if not summary:
            await run_blocking(self.record_failure, version_id, "压缩模型没有产出内容")
            return False
        # 压缩后 token 估算：按字符数粗估（中英混合约 2 字符/token），如实标记为估算
        return await run_blocking(
            self.record_result, version_id, summary, max(1, len(summary) // 2)
        )

    def record_result(self, version_id: str, summary: str, tokens_after: int) -> bool:
        """生成完成：落摘要，状态转 previewed。"""
        from dataclasses import replace

        return self._update(
            version_id,
            lambda v: replace(
                v,
                status=CompressionStatus.PREVIEWED,
                generated_summary=summary,
                tokens_after=tokens_after,
            ),
        )

    def record_failure(self, version_id: str, error: str) -> bool:
        """生成失败：状态转 failed，**不修改有效上下文**。"""
        from dataclasses import replace

        return self._update(
            version_id,
            lambda v: replace(v, status=CompressionStatus.FAILED, error=error),
        )

    def edit_summary(self, version_id: str, edited: str) -> bool:
        """用户编辑摘要（§7.1 编辑）。生成版保留，编辑版另存。"""
        with self._uow_factory() as uow:
            version = uow.compressions.get(version_id)
            if version is None:
                return False
            uow.compressions.update(version.with_edited_summary(edited))
            uow.commit()
            return True

    def accept(self, version_id: str) -> bool:
        """接受版本：状态转 accepted 并设为当前分支启用版本。"""
        with self._uow_factory() as uow:
            version = uow.compressions.get(version_id)
            if version is None or version.status is CompressionStatus.FAILED:
                return False
            from dataclasses import replace

            uow.compressions.update(replace(version, status=CompressionStatus.ACCEPTED))
            uow.compressions.set_active(version.branch_id, version_id)
            uow.commit()
            return True

    def reject(self, version_id: str) -> bool:
        from dataclasses import replace

        return self._update(version_id, lambda v: replace(v, status=CompressionStatus.REJECTED))

    def rollback(self, branch_id: str) -> bool:
        """回退：取消当前启用版本，回到未压缩状态。"""
        with self._uow_factory() as uow:
            if uow.compressions.get_active(branch_id) is None:
                return False
            uow.compressions.clear_active(branch_id)
            uow.commit()
            return True

    # ---------- 查询 ----------

    def list_versions(self, branch_id: str) -> list[CompressionVersion]:
        with self._uow_factory() as uow:
            return uow.compressions.list_for_branch(branch_id)

    def get_active(self, branch_id: str) -> CompressionVersion | None:
        with self._uow_factory() as uow:
            return uow.compressions.get_active(branch_id)

    def get(self, version_id: str) -> CompressionVersion | None:
        with self._uow_factory() as uow:
            return uow.compressions.get(version_id)

    # ---------- 内部 ----------

    def _update(
        self,
        version_id: str,
        transform: Callable[[CompressionVersion], CompressionVersion],
    ) -> bool:
        """读-改-写。``transform`` 是纯函数（如 ``lambda v: replace(v, status=...)``）。"""
        with self._uow_factory() as uow:
            version = uow.compressions.get(version_id)
            if version is None:
                return False
            uow.compressions.update(transform(version))
            uow.commit()
            return True
