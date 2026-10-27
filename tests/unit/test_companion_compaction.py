"""CMP-01 contracts: dated companionship memory and reversible, bounded distillation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from limbowave.domain.compaction import (
    COMPANION_PROMPT_VERSION,
    CompressionProfile,
    CompressionStatus,
    build_compression_prompt,
)
from limbowave.domain.companion_compaction import companion_batches
from limbowave.domain.configuration import CompressionSettings
from limbowave.domain.conversation import Message, MessageRole
from tests.unit.test_compression_service import T0
from tests.unit.test_compression_service import service as compression_service_fixture

service = compression_service_fixture


def message(content, *, id="m1", whitelisted=False):
    return Message(
        id=id,
        conversation_id="c1",
        branch_id="b1",
        role=MessageRole.USER,
        content=content,
        created_at=T0,
        is_whitelisted=whitelisted,
    )


def test_companion_template_preserves_life_and_boundaries():
    prompt = build_compression_prompt(
        goal="",
        tasks="",
        file_refs="",
        whitelist=["叫我小雨"],
        messages=["最近心情低落"],
        profile=CompressionProfile.COMPANION,
    )
    for term in (
        "生活事件",
        "人际关系",
        "交流偏好",
        "边界",
        "近期情绪",
        "长期",
        "时间",
        "禁止编造",
        "不要主动提起",
        "助手",
        "诊断",
        "来源",
        "叫我小雨",
    ):
        assert term in prompt
    assert "### 进展与任务" not in prompt


def test_profile_defaults_and_legacy_template():
    assert CompressionSettings().profile is CompressionProfile.COMPANION
    assert CompressionSettings(profile="task").profile is CompressionProfile.TASK
    with pytest.raises(ValueError):
        CompressionSettings(profile="unknown")
    prompt = build_compression_prompt(
        goal="部署", tasks="CI", file_refs="", whitelist=[], messages=[]
    )
    assert "### 进展与任务" in prompt


def test_companion_batches_are_bounded_and_keep_every_character():
    text = "搬家、朋友、心情。" * 1000
    batches = companion_batches([message(text)], max_chars=500)
    assert len(batches) > 1
    assert all(
        sum(map(len, batch.messages)) + sum(map(len, batch.whitelist)) <= 500 for batch in batches
    )
    joined = "".join(entry.split("\n", 1)[1] for batch in batches for entry in batch.messages)
    assert joined == text
    assert all("m1" in batch.messages[0] and "2026-09-20" in batch.messages[0] for batch in batches)


def test_whitelist_is_never_split_or_silently_trimmed():
    with pytest.raises(ValueError, match="白名单"):
        companion_batches([message("原文" * 300, whitelisted=True)], max_chars=500)
    batches = companion_batches([message("叫我小雨", whitelisted=True)], max_chars=500)
    assert "叫我小雨" in batches[0].whitelist[0]
    assert not batches[0].messages


def test_profile_survives_storage_edit_accept_and_rollback(service):
    old = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    assert old.profile is CompressionProfile.TASK
    service.record_result(old.id, "工程摘要", 20)
    service.accept(old.id)
    version = service.create_version(
        "c1",
        "b1",
        tokens_before=100,
        compression_model_id="m",
        compression_endpoint_id="e",
        profile=CompressionProfile.COMPANION,
    )
    assert version.prompt_version == COMPANION_PROMPT_VERSION
    service.record_result(version.id, "生活事件：搬家", 30)
    service.edit_summary(version.id, "用户修正后的生活事件")
    service.accept(version.id)
    loaded = service.get(version.id)
    assert loaded.profile is CompressionProfile.COMPANION
    assert loaded.generated_summary == "生活事件：搬家"
    assert loaded.effective_summary == "用户修正后的生活事件"
    assert service.accept(old.id)
    assert service.get_active("b1").id == old.id
    assert service.get(version.id).status is CompressionStatus.ACCEPTED
    assert service.rollback("b1")
    assert service.get_active("b1") is None
    assert len(service.list_versions("b1")) == 2


def test_companion_prompt_does_not_relabel_assistant_as_user(service):
    prompt = service.build_prompt("b1", profile=CompressionProfile.COMPANION)
    assert "2026-09-20" in prompt and "m1" in prompt
    assert "助手" in prompt and "先配 CI。" in prompt
    assert "密钥必须加密" in prompt


def test_unknown_prompt_version_is_not_silently_reinterpreted(service):
    version = service.create_version(
        "c1", "b1", tokens_before=1, compression_model_id="m", compression_endpoint_id="e"
    )
    with pytest.raises(ValueError, match="提示词版本"):
        _ = replace(version, prompt_version=999).profile


class BatchKernel:
    """Delegate fake kernel plumbing while keeping per-request output deterministic."""

    @staticmethod
    def make(outputs, *, fail_at=None):
        from tests.unit.test_compression_service import SummarizingKernel

        class Kernel(SummarizingKernel):
            def __init__(self):
                super().__init__()
                self.reset_count = 0

            async def new_session(self):
                self.reset_count += 1
                await super().new_session()

            async def send_message(self, text, *, images=None):
                if fail_at == len(self.prompts):
                    raise RuntimeError("第二段失败")
                self._summary = outputs[min(len(self.prompts), len(outputs) - 1)]
                await super().send_message(text, images=images)

            def subscribe(self, handler):
                self._handlers.append(handler)
                return lambda: self._handlers.remove(handler)

        return Kernel()


def companion_version(service):
    return service.create_version(
        "c1",
        "b1",
        tokens_before=100,
        compression_model_id="m",
        compression_endpoint_id="e",
        profile=CompressionProfile.COMPANION,
    )


def add_long_history(service):
    with service._uow_factory() as uow:
        uow.messages.add(message("生活事件-搬家。" * 8000, id="long"))
        uow.commit()


async def test_multibatch_carries_summary_resets_kernel_and_publishes_only_final(service):
    add_long_history(service)
    version = companion_version(service)
    kernel = BatchKernel.make(["中间记忆：边界仍有效", "合并记忆：小雨最近搬家"])
    assert await service.generate(version.id, kernel)
    assert len(kernel.prompts) > 1
    assert kernel.reset_count == len(kernel.prompts) - 1
    assert "中间记忆：边界仍有效" in kernel.prompts[1]
    assert max(map(len, kernel.prompts)) < 31000
    assert not kernel._handlers
    stored = service.get(version.id)
    assert stored.status is CompressionStatus.PREVIEWED
    assert "合并记忆" in stored.generated_summary
    assert "密钥必须加密" in stored.generated_summary  # model omission repaired verbatim
    assert len(service.list_versions("b1")) == 1
    assert service.get_active("b1") is None


async def test_failed_later_batch_keeps_active_and_no_partial_summary(service):
    active = companion_version(service)
    service.record_result(active.id, "已接受记忆", 10)
    service.accept(active.id)
    add_long_history(service)
    version = companion_version(service)
    kernel = BatchKernel.make(["仅中间结果"], fail_at=1)
    assert not await service.generate(version.id, kernel)
    stored = service.get(version.id)
    assert stored.status is CompressionStatus.FAILED
    assert stored.generated_summary == ""
    assert service.get_active("b1").id == active.id
    assert not kernel._handlers


async def test_accepted_user_edits_seed_incremental_compaction(service):
    active = companion_version(service)
    service.record_result(active.id, "未经编辑的摘要", 10)
    service.edit_summary(active.id, "用户纠正：请叫我小雨，别主动提起旧工作")
    service.accept(active.id)
    with service._uow_factory() as uow:
        uow.messages.add(replace(message("今天搬家了", id="next"), created_at=T0.replace(hour=13)))
        uow.commit()
    version = companion_version(service)
    kernel = BatchKernel.make(["搬家；保留称呼和边界"])
    assert await service.generate(version.id, kernel)
    assert len(kernel.prompts) == 1
    assert "用户纠正：请叫我小雨" in kernel.prompts[0]
    assert "今天搬家了" in kernel.prompts[0]
    assert "怎么部署？" not in kernel.prompts[0]
    assert service.get_active("b1").id == active.id  # new preview not auto-accepted


async def test_cancel_keeps_active_and_records_failure(service):
    import asyncio

    from tests.unit.test_run_coordinator import FakeKernel

    version = companion_version(service)
    entered = asyncio.Event()

    class WaitingKernel(FakeKernel):
        async def send_message(self, text, *, images=None):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(service.generate(version.id, WaitingKernel()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.get(version.id).status is CompressionStatus.FAILED
    assert service.get_active("b1") is None


async def test_oversized_output_is_failed_not_truncated(service):
    version = companion_version(service)
    assert not await service.generate(version.id, BatchKernel.make(["过长" * 7000]))
    assert service.get(version.id).generated_summary == ""
    assert "预算" in service.get(version.id).error


async def test_snapshot_excludes_messages_added_after_preview(service):
    version = companion_version(service)
    with service._uow_factory() as uow:
        uow.messages.add(
            replace(message("不在预览范围的后续消息", id="tail"), created_at=T0.replace(hour=13))
        )
        uow.commit()
    kernel = BatchKernel.make(["摘要"])
    assert await service.generate(version.id, kernel)
    assert "不在预览范围的后续消息" not in kernel.prompts[0]
    assert "tail" not in service.get(version.id).input_message_ids


async def test_generation_cannot_overwrite_an_accepted_version(service):
    version = companion_version(service)
    service.record_result(version.id, "历史版本不可被重新生成覆盖", 20)
    service.accept(version.id)
    kernel = BatchKernel.make(["不应写入"])
    assert not await service.generate(version.id, kernel)
    assert not kernel.prompts
    assert service.get(version.id).generated_summary == "历史版本不可被重新生成覆盖"


async def test_truncated_model_output_is_not_a_preview(service):
    from tests.unit.test_run_coordinator import FakeKernel

    class TruncatedKernel(FakeKernel):
        async def send_message(self, text, *, images=None):
            self.say("未完成的边界记录", stop="length")
            self.emit("run.settled", {})

    version = companion_version(service)
    assert not await service.generate(version.id, TruncatedKernel())
    assert service.get(version.id).status is CompressionStatus.FAILED
    assert "截断" in service.get(version.id).error
    assert service.get(version.id).generated_summary == ""



async def test_history_versions_reject_late_generation_and_failure_callbacks(service):
    version = companion_version(service)
    service.record_result(version.id, "已接受历史", 10)
    service.accept(version.id)
    kernel = BatchKernel.make(["不应使用"])
    assert not await service.generate_isolated(version.id, kernel)
    assert not service.record_failure(version.id, "迟到的失败")
    assert not service.record_result(version.id, "迟到的内容", 20)
    assert not service.reject(version.id)
    assert service.get_active("b1").generated_summary == "已接受历史"
    assert service.get(version.id).status is CompressionStatus.ACCEPTED


def test_unready_or_blank_summary_cannot_be_accepted(service):
    version = companion_version(service)
    assert not service.accept(version.id)
    service.record_result(version.id, "  ", 0)
    assert not service.accept(version.id)
    assert service.get_active("b1") is None



async def test_single_batch_timeout_includes_send_and_unsubscribes():
    import asyncio

    from limbowave.application.services.compression_service import CompressionService

    kernel = BatchKernel.make(["unused"])

    async def stalled_send(*args, **kwargs):
        await asyncio.Event().wait()

    kernel.send_message = stalled_send
    with pytest.raises(TimeoutError):
        await CompressionService._request_summary(kernel, "bounded prompt", timeout=0.01)
    assert not kernel._handlers
