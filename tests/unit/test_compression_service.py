"""上下文压缩验收（Phase 5 / 设计计划 §七）。

锁住的不变量：
- 估算是估算：展示文本带「估算」字样，内核不支持时如实显示「未知」；
- 阈值分级：70/80/90 三档动作；
- 白名单：标记的消息原文进提示词的「白名单原文」节，不进待压缩区；
- 提示词含铁律（禁止编造 + 白名单原文不动）；
- 版本化：生成/编辑/接受/回退，每步都留版本，失败不改有效上下文；
- 同一分支至多一个启用版本；
- 接受/回退/编辑/重试都跑在 SQLite 与内存两个后端上。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.application.kernel import ContextUsage
from limbowave.application.services.compression_service import (
    CompressionService,
    ThresholdAction,
    threshold_action,
)
from limbowave.domain.compaction import CompressionStatus, build_compression_prompt
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def service(request, tmp_path: Path, vault_key: VaultKey):
    """同一组语义跑在两种后端上。"""
    if request.param == "memory":
        factory = in_memory_uow_factory(InMemoryStore())
    else:
        factory = sqlite_uow_factory(tmp_path / "c.db", vault_key)
    svc = CompressionService(factory)
    _seed(factory)
    return svc


def _seed(factory) -> None:
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="部署", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="怎么部署？",
                created_at=T0,
            )
        )
        uow.messages.add(
            Message(
                id="m2",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="先配 CI。",
                created_at=T0.replace(minute=1),
            )
        )
        uow.messages.add(
            Message(
                id="m3",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="密钥必须加密",
                created_at=T0.replace(minute=2),
                is_whitelisted=True,
            )
        )
        uow.commit()


# ---------- 估算（Task 5.1） ----------


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        (50.0, ThresholdAction.NONE),
        (70.0, ThresholdAction.HINT),
        (80.0, ThresholdAction.PREVIEW),
        (90.0, ThresholdAction.BLOCK),
        (95.5, ThresholdAction.BLOCK),
    ],
)
def test_threshold_action(percent: float, expected: ThresholdAction) -> None:
    assert threshold_action(percent) is expected


async def test_estimate_marks_estimate(service: CompressionService) -> None:
    """展示文本必须带「估算」——不伪装成精确值。"""

    class UsageKernel(FakeKernel):
        async def get_context_usage(self) -> ContextUsage:
            return ContextUsage(tokens=42000, context_window=128000, percent=32.8)

    report = await service.estimate(UsageKernel())
    assert "估算" in report.display
    assert report.usage is not None and report.usage.percent == 32.8
    assert report.action is ThresholdAction.NONE


async def test_estimate_unknown_when_unsupported(service: CompressionService) -> None:
    """内核不支持估算：如实显示「未知」，不编数字。"""
    report = await service.estimate(FakeKernel())  # 默认返回 None
    assert report.usage is None
    assert report.display == "未知"
    assert report.action is ThresholdAction.NONE


async def test_estimate_none_kernel(service: CompressionService) -> None:
    report = await service.estimate(None)
    assert report.usage is None


# ---------- 白名单（§7.3） ----------


def test_toggle_whitelist(service: CompressionService) -> None:
    assert service.toggle_whitelist("m1") is True
    assert service.toggle_whitelist("m1") is False  # 再切回来
    # 不存在消息
    assert service.toggle_whitelist("nope") is False


def test_whitelisted_messages(service: CompressionService) -> None:
    whitelisted = service.whitelisted_messages("b1")
    assert [m.id for m in whitelisted] == ["m3"]


def test_whitelist_survives_in_prompt(service: CompressionService) -> None:
    """压缩提示词：白名单消息在「白名单原文」节，且不在待压缩区。"""
    prompt = service.build_prompt("b1", goal="部署到生产", tasks="配置 CI")
    # 白名单原文在
    assert "密钥必须加密" in prompt
    # 待压缩区不含白名单（m3 不在 messages 区重复出现为「用户: 密钥必须加密」）
    compressible_section = prompt.split("## 待压缩消息")[-1]
    assert "密钥必须加密" not in compressible_section
    # 铁律在
    assert "禁止编造" in prompt
    assert "一个字都不能改" in prompt
    # 目标与任务在
    assert "部署到生产" in prompt
    assert "配置 CI" in prompt


def test_build_prompt_pure() -> None:
    """提示词组装是纯函数：同输入同输出，无白名单时如实标注。"""
    prompt = build_compression_prompt(
        goal="", tasks="", file_refs="", whitelist=[], messages=["用户: 你好"]
    )
    assert "（无）" in prompt or "（未明确）" in prompt
    assert "用户: 你好" in prompt


# ---------- 版本管理（Task 5.3） ----------


def test_create_version_captures_scope(service: CompressionService) -> None:
    v = service.create_version(
        "c1",
        "b1",
        tokens_before=42000,
        compression_model_id="glm-4.6",
        compression_endpoint_id="relay-a",
    )
    assert v.status is CompressionStatus.DRAFT
    assert set(v.input_message_ids) == {"m1", "m2", "m3"}  # 输入范围 = 分支全部消息
    assert v.whitelist_message_ids == ("m3",)  # 白名单快照
    assert v.prompt_version >= 1


def test_record_result_transitions_to_previewed(service: CompressionService) -> None:
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    assert service.record_result(v.id, "摘要内容", 50)
    updated = service.get(v.id)
    assert updated is not None
    assert updated.status is CompressionStatus.PREVIEWED
    assert updated.generated_summary == "摘要内容"
    assert updated.tokens_after == 50


def test_edit_keeps_both_versions(service: CompressionService) -> None:
    """编辑不覆盖生成版——两个都留着（§7.4 生成结果 + 用户编辑后的结果）。"""
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v.id, "生成版", 50)
    service.edit_summary(v.id, "编辑版")
    updated = service.get(v.id)
    assert updated is not None
    assert updated.generated_summary == "生成版"  # 原样保留
    assert updated.edited_summary == "编辑版"
    assert updated.effective_summary == "编辑版"  # 生效的是编辑版


def test_accept_sets_active(service: CompressionService) -> None:
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v.id, "摘要", 50)
    assert service.accept(v.id)
    active = service.get_active("b1")
    assert active is not None and active.id == v.id
    assert active.status is CompressionStatus.ACCEPTED


def test_accept_failed_version_refused(service: CompressionService) -> None:
    """失败的版本不能被接受——失败不修改有效上下文。"""
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_failure(v.id, "模型超时")
    assert not service.accept(v.id)
    assert service.get_active("b1") is None


def test_failure_does_not_touch_active(service: CompressionService) -> None:
    """已有启用版本时，新版本失败不影响它。"""
    v1 = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v1.id, "第一版摘要", 50)
    service.accept(v1.id)

    v2 = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_failure(v2.id, "生成失败")

    active = service.get_active("b1")
    assert active is not None and active.id == v1.id  # 启用版本没变


def test_rollback_clears_active(service: CompressionService) -> None:
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v.id, "摘要", 50)
    service.accept(v.id)
    assert service.rollback("b1")
    assert service.get_active("b1") is None
    # 版本记录还在（历史不丢），只是不再启用
    assert service.get(v.id) is not None


def test_rollback_without_active_is_noop(service: CompressionService) -> None:
    assert not service.rollback("b1")


def test_only_one_active_per_branch(service: CompressionService) -> None:
    """同一分支至多一个启用版本：接受新版本顶掉旧的。"""
    v1 = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v1.id, "第一版", 50)
    service.accept(v1.id)
    v2 = service.create_version(
        "c1", "b1", tokens_before=90, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_result(v2.id, "第二版", 45)
    service.accept(v2.id)

    active = service.get_active("b1")
    assert active is not None and active.id == v2.id
    # 两版都还在历史里
    assert len(service.list_versions("b1")) == 2


# ---------- 生成编排（Task 5.2 的执行） ----------


class SummarizingKernel(FakeKernel):
    """模拟压缩专用模型：收到 prompt 后回一段摘要。"""

    def __init__(self, summary: str = "### 白名单原文\n密钥必须加密\n### 会话目标\n部署") -> None:
        super().__init__()
        self._summary = summary
        self.prompts: list[str] = []

    async def send_message(self, text: str, *, images=None) -> None:
        self.prompts.append(text)
        await super().send_message(text, images=images)
        # 模拟内核事件：message.end + run.settled
        self.emit(
            "message.end",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": self._summary}],
                }
            },
        )
        self.emit("run.settled", {})


async def test_generate_records_summary(service: CompressionService) -> None:
    v = service.create_version(
        "c1",
        "b1",
        tokens_before=42000,
        compression_model_id="glm-4.6",
        compression_endpoint_id="relay-a",
    )
    kernel = SummarizingKernel()
    assert await service.generate(v.id, kernel, goal="部署到生产", tasks="配置 CI")

    updated = service.get(v.id)
    assert updated is not None
    assert updated.status is CompressionStatus.PREVIEWED
    assert "密钥必须加密" in updated.generated_summary  # 白名单原文进了摘要
    assert updated.tokens_after > 0
    # 发给压缩模型的提示词含铁律与目标
    assert "禁止编造" in kernel.prompts[0]
    assert "部署到生产" in kernel.prompts[0]


async def test_generate_failure_marks_failed(service: CompressionService) -> None:
    """压缩模型不出内容：落 failed，原上下文不变。"""
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )

    class EmptyKernel(FakeKernel):
        async def send_message(self, text: str, *, images=None) -> None:
            await super().send_message(text, images=images)
            self.emit("run.settled", {})  # 没有 message.end

    assert not await service.generate(v.id, EmptyKernel(), timeout=0.2)
    updated = service.get(v.id)
    assert updated is not None
    assert updated.status is CompressionStatus.FAILED
    assert "没有产出内容" in (updated.error or "")
    assert service.get_active("b1") is None  # 有效上下文未变


async def test_generate_send_error_marks_failed(service: CompressionService) -> None:
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    kernel = FakeKernel()
    kernel.send_error = RuntimeError("连接超时")
    assert not await service.generate(v.id, kernel)
    updated = service.get(v.id)
    assert updated is not None
    assert updated.status is CompressionStatus.FAILED


# ---------- 阈值可配置与自动触发（§七.1） ----------


def test_execution_thresholds_are_configurable() -> None:
    """§七.1：支持全局配置触发阈值——不是写死的 70/80/90。"""
    from limbowave.domain.configuration import CompressionSettings

    strict = CompressionSettings(hint_threshold=50, preview_threshold=60, block_threshold=70)
    assert (
        threshold_action(
            55,
            hint=strict.hint_threshold,
            preview=strict.preview_threshold,
            block=strict.block_threshold,
        )
        is ThresholdAction.HINT
    )
    assert (
        threshold_action(
            65,
            hint=strict.hint_threshold,
            preview=strict.preview_threshold,
            block=strict.block_threshold,
        )
        is ThresholdAction.PREVIEW
    )
    assert (
        threshold_action(
            75,
            hint=strict.hint_threshold,
            preview=strict.preview_threshold,
            block=strict.block_threshold,
        )
        is ThresholdAction.BLOCK
    )


def test_thresholds_must_be_ordered() -> None:
    """阈值必须递增，否则分级语义就乱了——配置层直接拒绝。"""
    from limbowave.domain.configuration import CompressionSettings

    with pytest.raises(ValueError, match="hint ≤ preview ≤ block"):
        CompressionSettings(hint_threshold=90, preview_threshold=80, block_threshold=70)


def test_default_thresholds_match_plan() -> None:
    """默认值取计划书建议的 70/80/90。"""
    from limbowave.domain.configuration import CompressionSettings

    defaults = CompressionSettings()
    assert (defaults.hint_threshold, defaults.preview_threshold, defaults.block_threshold) == (
        70.0,
        80.0,
        90.0,
    )
    assert defaults.auto_preview is True


def test_config_carries_compression_settings() -> None:
    """阈值随权威配置落盘，可被用户编辑。"""
    from limbowave.domain.configuration import AppConfiguration, CompressionSettings

    config = AppConfiguration(compression=CompressionSettings(block_threshold=85))
    payload = config.model_dump(mode="json")
    assert payload["compression"]["block_threshold"] == 85
    restored = AppConfiguration.model_validate(payload)
    assert restored.compression.block_threshold == 85


async def test_estimate_honours_configured_thresholds(service: CompressionService) -> None:
    """占用 75%：默认阈值下只是 HINT，收紧阈值后应判 BLOCK。"""
    from limbowave.domain.configuration import CompressionSettings

    class UsageKernel(FakeKernel):
        async def get_context_usage(self):
            from limbowave.application.kernel import ContextUsage

            return ContextUsage(tokens=96000, context_window=128000, percent=75.0)

    default_report = await service.estimate(UsageKernel())
    assert default_report.action is ThresholdAction.HINT

    strict = CompressionSettings(hint_threshold=50, preview_threshold=60, block_threshold=70)
    strict_report = await service.estimate(UsageKernel(), settings=strict)
    assert strict_report.action is ThresholdAction.BLOCK
