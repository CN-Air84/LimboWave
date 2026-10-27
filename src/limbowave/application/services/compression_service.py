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
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from limbowave.application.background import run_blocking
from limbowave.application.branch_path import branch_messages
from limbowave.application.kernel import AgentKernel, ContextUsage, KernelEvent
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.privacy_service import load_policy, privacy_model_request
from limbowave.domain.compaction import (
    CompressionProfile,
    CompressionStatus,
    CompressionVersion,
    build_compression_prompt,
    compression_prompt_version,
)
from limbowave.domain.companion_compaction import (
    COMPANION_SUMMARY_CHARS,
    CompanionBatch,
    companion_batches,
    source_label,
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
        profile: CompressionProfile = CompressionProfile.TASK,
    ) -> CompressionVersion:
        """创建一个新的压缩版本（draft）。输入范围 = 当前分支完整对话（含继承前缀）。"""
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None or branch.conversation_id != conversation_id:
                raise ValueError("压缩分支不属于当前会话")
            if load_policy(uow, branch.conversation_id if branch else None).rules:
                raise ValueError("话题禁区已启用：旧摘要来源不完整，暂不生成或复用压缩摘要。")
            messages = branch_messages(uow, branch_id)
            whitelist = tuple(m.id for m in messages if m.is_whitelisted)
            # Sort key is (created_at, id) and id is random: on coarse clocks two versions
            # created back-to-back can share a timestamp and flip order. Force a strict order.
            created_at = datetime.now(UTC)
            existing = uow.compressions.list_for_branch(branch_id)
            if existing:
                latest = max(v.created_at for v in existing)
                if created_at <= latest:
                    created_at = latest + timedelta(microseconds=1)
            version = CompressionVersion(
                id=f"cmp_{uuid4().hex[:16]}",
                conversation_id=conversation_id,
                branch_id=branch_id,
                created_at=created_at,
                status=CompressionStatus.DRAFT,
                input_message_ids=tuple(m.id for m in messages),
                tokens_before=tokens_before,
                compression_model_id=compression_model_id,
                compression_endpoint_id=compression_endpoint_id,
                prompt_version=compression_prompt_version(profile),
                whitelist_message_ids=whitelist,
            )
            uow.compressions.add(version)
            uow.commit()
            return version

    def _prompt_messages(
        self, branch_id: str, message_ids: tuple[str, ...] | None = None,
    ) -> list[Message]:
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None:
                raise ValueError("压缩分支不存在")
            if load_policy(uow, branch.conversation_id).rules:
                raise ValueError("话题禁区已启用：旧摘要来源不完整，暂不生成或复用压缩摘要。")
            messages = branch_messages(uow, branch_id)
        if message_ids is not None:
            if tuple(m.id for m in messages[:len(message_ids)]) != message_ids:
                raise ValueError("压缩版本的消息范围已变化，请重新生成预览")
            messages = messages[:len(message_ids)]
        return messages

    def build_prompt(
        self,
        branch_id: str,
        *,
        goal: str = "",
        tasks: str = "",
        message_ids: tuple[str, ...] | None = None,
        profile: CompressionProfile = CompressionProfile.TASK,
    ) -> str:
        """Task compatibility entry; companion generation uses bounded batches below."""
        messages = self._prompt_messages(branch_id, message_ids)
        companion = CompressionProfile(profile) is CompressionProfile.COMPANION
        whitelist_texts = [
            (source_label(m) + "\n" if companion else "") + m.content
            for m in messages if m.is_whitelisted
        ]
        file_refs = sorted({f"[{m.id}]" for m in messages if m.role is MessageRole.USER})
        compressible = [
            (source_label(m) + "\n" if companion else
             f"{'用户' if m.role is MessageRole.USER else '助手'}: ") + m.content
            for m in messages if not m.is_whitelisted
        ]
        return build_compression_prompt(
            goal=goal, tasks=tasks, file_refs=", ".join(file_refs),
            whitelist=whitelist_texts, messages=compressible, profile=profile,
        )

    def _companion_input(
        self, version: CompressionVersion,
    ) -> tuple[list[CompanionBatch], str, list[str]]:
        messages = self._prompt_messages(version.branch_id, version.input_message_ids)
        # Respect both the preview-time whitelist snapshot and newly pinned messages.
        from dataclasses import replace

        messages = [replace(m, is_whitelisted=True)
                    if m.id in version.whitelist_message_ids else m for m in messages]
        whitelist = [m.content for m in messages if m.is_whitelisted and m.content]
        if sum(map(len, whitelist)) > COMPANION_SUMMARY_CHARS:
            raise ValueError("白名单原文超过摘要预算，请先调整白名单；未截断任何原文")
        previous = ""
        active = self.get_active(version.branch_id)
        if (active is not None and active.status is CompressionStatus.ACCEPTED
                and active.profile is CompressionProfile.COMPANION
                and active.input_message_ids
                and version.input_message_ids[:len(active.input_message_ids)]
                == active.input_message_ids):
            previous = active.effective_summary.strip()
            if previous:
                if len(previous) > COMPANION_SUMMARY_CHARS:
                    raise ValueError("已启用摘要超过分段预算，请先编辑缩短；未丢弃用户编辑")
                messages = messages[len(active.input_message_ids):]
        return companion_batches(messages), previous, whitelist

    async def generate_isolated(
        self,
        version_id: str,
        source: AgentKernel,
        *,
        timeout: float = 120.0,
        on_event: Callable[[KernelEvent], None] | None = None,
    ) -> bool:
        """Generate a preview in a disposable kernel; never fall back to the live chat."""
        import asyncio
        import contextlib

        version = await run_blocking(self.get, version_id)
        if version is None or version.status is not CompressionStatus.DRAFT:
            return False

        isolated: AgentKernel | None = None
        unsubscribe: Callable[[], None] | None = None
        try:
            async with asyncio.timeout(timeout):
                isolated = source.create_isolated()
                if isolated is None or isolated is source:
                    isolated = None
                    raise RuntimeError("当前内核不支持隔离压缩，未向主会话发送请求")
                if on_event is not None:
                    unsubscribe = isolated.subscribe(on_event)
                state = await source.get_state()
                await isolated.start()
                if state.provider and state.model_id:
                    await isolated.set_model(state.provider, state.model_id)
                await isolated.set_thinking_level(state.thinking_level)
            return await self.generate(version_id, isolated, timeout=timeout)
        except asyncio.CancelledError:
            await run_blocking(self.record_failure, version_id, "压缩已取消")
            raise
        except Exception as exc:
            error = "压缩生成超时" if isinstance(exc, TimeoutError) else redact_text(str(exc))
            await run_blocking(self.record_failure, version_id, error)
            return False
        finally:
            if unsubscribe is not None:
                unsubscribe()
            if isolated is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(isolated.shutdown(), timeout=10.0)

    async def generate(
        self, version_id: str, kernel: AgentKernel, *, goal: str = "", tasks: str = "",
        timeout: float = 120.0,
    ) -> bool:
        with privacy_model_request():
            return await self._generate(version_id, kernel, goal=goal, tasks=tasks, timeout=timeout)

    async def _generate(
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
        if version is None or version.status is not CompressionStatus.DRAFT:
            return False

        try:
            batches: list[CompanionBatch] = []
            summary = ""
            whitelist: list[str] = []
            if version.profile is CompressionProfile.COMPANION:
                batches, summary, whitelist = await run_blocking(self._companion_input, version)
            # Allow long-history distillation more than a single model response's time,
            # while retaining a bounded total duration and per-request timeout.
            async with asyncio.timeout(min(timeout * max(1, len(batches)), 600.0)):
                if version.profile is CompressionProfile.COMPANION:
                    for index, batch in enumerate(batches):
                        if index:
                            # No accumulated raw history or previous model output in the kernel.
                            await kernel.new_session()
                        prompt = build_compression_prompt(
                            goal=goal, tasks=tasks, file_refs="",
                            whitelist=list(batch.whitelist), messages=list(batch.messages),
                            profile=version.profile, previous_summary=summary,
                        )
                        summary = await self._request_summary(kernel, prompt, timeout=timeout)
                        if len(summary) > COMPANION_SUMMARY_CHARS:
                            raise ValueError("陪伴摘要超过分段预算，请重试或调整白名单；未截断摘要")
                    missing = [text for text in whitelist if text not in summary]
                    if missing:
                        summary += "\n\n### 白名单原文（补全）\n" + "\n\n".join(missing)
                    if len(summary) > COMPANION_SUMMARY_CHARS:
                        raise ValueError("白名单与摘要合并后超过预算，请调整白名单后重试")
                else:
                    prompt = await run_blocking(
                        self.build_prompt, version.branch_id, goal=goal, tasks=tasks,
                        message_ids=version.input_message_ids, profile=version.profile,
                    )
                    summary = await self._request_summary(kernel, prompt, timeout=timeout)
                # Recheck boundaries before publishing. An intermediate batch is never a preview.
                await run_blocking(
                    self._prompt_messages, version.branch_id, version.input_message_ids
                )
                return await run_blocking(
                    self.record_result, version_id, summary, max(1, len(summary) // 2)
                )
        except asyncio.CancelledError:
            await run_blocking(self.record_failure, version_id, "压缩已取消")
            raise
        except Exception as exc:
            await run_blocking(
                self.record_failure, version_id,
                "压缩生成超时" if isinstance(exc, TimeoutError) else redact_text(str(exc)),
            )
            return False

    @staticmethod
    async def _request_summary(kernel: AgentKernel, prompt: str, *, timeout: float) -> str:
        import asyncio

        chunks: list[str] = []
        settled = asyncio.Event()
        runtime_error: list[str] = []

        def _on_event(event: object) -> None:
            kind = getattr(event, "kind", "")
            payload = getattr(event, "payload", {}) or {}
            if kind == "message.end":
                message = payload.get("message") or {}
                if message.get("role") == "assistant":
                    if message.get("stopReason") == "length":
                        runtime_error.append("压缩模型输出被截断，请调整输出上限后重新生成")
                        settled.set()
                        return
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
            async with asyncio.timeout(timeout):
                await kernel.send_message(prompt)
                await settled.wait()
                if runtime_error:
                    raise RuntimeError(runtime_error[0])
        finally:
            unsubscribe()
        summary = "".join(chunks).strip()
        if not summary:
            raise ValueError("压缩模型没有产出内容")
        return summary

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
            expected_status=CompressionStatus.DRAFT,
        )

    def record_failure(self, version_id: str, error: str) -> bool:
        """生成失败：状态转 failed，**不修改有效上下文**。"""
        from dataclasses import replace

        return self._update(
            version_id,
            lambda v: replace(v, status=CompressionStatus.FAILED, error=error),
            expected_status=CompressionStatus.DRAFT,
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
            if (
                version is None
                or version.status not in {CompressionStatus.PREVIEWED, CompressionStatus.ACCEPTED}
                or not version.effective_summary.strip()
            ):
                return False
            from dataclasses import replace

            uow.compressions.update(replace(version, status=CompressionStatus.ACCEPTED))
            uow.compressions.set_active(version.branch_id, version_id)
            uow.commit()
            return True

    def reject(self, version_id: str) -> bool:
        from dataclasses import replace

        return self._update(
            version_id, lambda v: replace(v, status=CompressionStatus.REJECTED),
            expected_status=CompressionStatus.PREVIEWED,
        )

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
        *, expected_status: CompressionStatus | None = None,
    ) -> bool:
        """读-改-写。``transform`` 是纯函数（如 ``lambda v: replace(v, status=...)``）。"""
        with self._uow_factory() as uow:
            version = uow.compressions.get(version_id)
            if (version is None
                    or (expected_status is not None and version.status is not expected_status)):
                return False
            uow.compressions.update(transform(version))
            uow.commit()
            return True
