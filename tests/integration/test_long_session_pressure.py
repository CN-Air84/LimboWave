"""长会话压力测试（Task 10.2 / Task 0.2 的性能基线）。

规模按计划书要求：**2,000 条消息、20 个分支、500 个工具调用、多次压缩与回退、
大量请求日志、多站点切换**。

这里不做「基准数字必须小于 X」的脆弱断言——不同机器差异大，写死阈值只会
变成随机失败。断言的是**复杂度性质**（不该出现的平方级行为）与**可用性门槛**
（宽松但真实的上限），同时把实测数字打印出来作为性能基线记录。

关键风险点（实测结论）：字段级加密意味着**读 2,000 条消息要解约 6,000 次密**
（正文 + 思考 + 工具步骤）。**实测这不是问题**——ChaCha20-Poly1305 对百字节级
载荷的吞吐极高，2,000 条全量读取约 36ms（见 ``test_baseline_recorded`` 的输出）。
原先担心的「逐条解密会成为规模瓶颈」在本规模上不成立；真正需要留意的是
**界面侧的物化数量**（每条消息一个富文本组件），那由 ChatView 的滞性化解决。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.request_log_service import RequestLogService
from limbowave.domain.compaction import CompressionStatus, CompressionVersion
from limbowave.domain.conversation import (
    Branch,
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from limbowave.domain.permissions import Capability
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot
from limbowave.domain.tool_step import ToolStatus, ToolStep
from limbowave.infrastructure.crypto.vault import KdfParams, Vault, VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

MESSAGE_COUNT = 2000
BRANCH_COUNT = 20
TOOL_CALL_COUNT = 500
RUN_COUNT = 500  # 请求日志规模
COMPRESSION_VERSIONS = 12

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)

# 采集到的基线（供文档记录）
_BASELINE: dict[str, float] = {}


@pytest.fixture(scope="module")
def vault_key(tmp_path_factory: pytest.TempPathFactory) -> VaultKey:
    path = tmp_path_factory.mktemp("vault") / "v.json"
    return Vault(path, params=KdfParams(time_cost=1, memory_cost=8, parallelism=1)).create("p")


@pytest.fixture(scope="module")
def big_db(tmp_path_factory: pytest.TempPathFactory, vault_key: VaultKey) -> Path:
    """灌一个规模符合要求的长会话库。一次性构建，多个用例共用。"""
    root = tmp_path_factory.mktemp("long-session")
    db_path = root / "big.db"
    factory = sqlite_uow_factory(db_path, vault_key)

    started = time.perf_counter()
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="长会话", created_at=T0))
        # 20 个分支：b0 是承载全部 2,000 条的主线，其余从它分叉各带一小段。
        # b0 的 created_at 必须**最晚**——open_conversation 取「最新分支」，
        # 否则测的就不是主线（第一版压测踩过这个坑：数字快得可疑）。
        branches = [Branch(id="b0", conversation_id="c1", created_at=T0 + timedelta(days=1))]
        for index in range(1, BRANCH_COUNT):
            branches.append(
                Branch(
                    id=f"b{index}",
                    conversation_id="c1",
                    created_at=T0 + timedelta(minutes=index),
                    parent_branch_id="b0",
                    forked_from_message_id=f"m{index * 7}",
                )
            )
        for branch in branches:
            uow.branches.add(branch)

        # 2,000 条消息：主线占绝大多数，其余分支各分一小段
        messages: list[Message] = []
        tool_steps_attached = 0
        for index in range(MESSAGE_COUNT):
            branch_id = "b0"  # 主线承载全部 2,000 条
            role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            steps: tuple[ToolStep, ...] = ()
            # 前 500 条助手消息带工具步骤（合计 500 个工具调用）
            if role is MessageRole.ASSISTANT and tool_steps_attached < TOOL_CALL_COUNT:
                steps = (
                    ToolStep(
                        tool_call_id=f"call-{index}",
                        name="read" if index % 4 else "bash",
                        status=ToolStatus.OK,
                        duration_ms=10 + index % 50,
                        args={"path": f"file-{index}.txt"},
                        result_summary=f"结果 {index}",
                    ),
                )
                tool_steps_attached += 1
            messages.append(
                Message(
                    id=f"m{index}",
                    conversation_id="c1",
                    branch_id=branch_id,
                    role=role,
                    content=f"第 {index} 条消息的内容，包含中文与 English mixed text。",
                    created_at=T0 + timedelta(seconds=index),
                    run_id=f"r{index}",
                    status=MessageStatus.COMPLETE,
                    thinking="思考" * (5 if index % 10 else 40),
                    is_whitelisted=index % 250 == 0,
                    tool_steps=steps,
                )
            )
        for message in messages:
            uow.messages.add(message)
        # 每个分叉分支各放 2 条（否则 20 个分支里 19 个是空的，不算真覆盖）
        for index in range(1, BRANCH_COUNT):
            for offset in range(2):
                uow.messages.add(
                    Message(
                        id=f"fork{index}-{offset}",
                        conversation_id="c1",
                        branch_id=f"b{index}",
                        role=MessageRole.USER if offset == 0 else MessageRole.ASSISTANT,
                        content=f"分支 {index} 的第 {offset} 条",
                        created_at=T0 + timedelta(minutes=index, seconds=offset),
                        status=MessageStatus.COMPLETE,
                    )
                )

        # 500 条运行记录 + 意图/传输快照（请求日志规模）
        for index in range(RUN_COUNT):
            run_id = f"r{index}"
            branch_id = messages[index].branch_id
            uow.runs.add(
                RunRecord(
                    id=run_id,
                    conversation_id="c1",
                    branch_id=branch_id,
                    status=RunStatus.COMPLETED,
                    created_at=T0 + timedelta(seconds=index),
                    user_message_id=f"m{index * 2}",
                    assistant_message_id=f"m{index * 2 + 1}",
                    stop_reason="stop",
                )
            )
            uow.snapshots.add_intent(
                RequestIntentSnapshot(
                    id=f"i{index}",
                    run_id=run_id,
                    conversation_id="c1",
                    branch_id=branch_id,
                    logical_model_id="deepseek-chat",
                    # 多站点切换：轮流落到不同端点
                    endpoint_id=["relay-a", "relay-b", "relay-c"][index % 3],
                    routing_reason="默认绑定",
                    created_at=T0 + timedelta(seconds=index),
                    app_params={"model": "deepseek-chat", "max_tokens": 4096},
                )
            )
            uow.snapshots.add_transport(
                TransportSnapshot(
                    id=f"t{index}",
                    run_id=run_id,
                    created_at=T0 + timedelta(seconds=index),
                    sequence=1,
                    url=f"https://relay-{index % 3}.example.com/v1/chat/completions",
                    headers={
                        "Authorization": {
                            "present": True,
                            "scheme": "Bearer",
                            "value": "[REDACTED]",
                        }
                    },
                    body={"model": "deepseek-chat", "messages": []},
                    response_status=200,
                    stop_reason="stop",
                )
            )

        # 多次压缩与回退：12 个版本，交替接受/回退
        for index in range(COMPRESSION_VERSIONS):
            version = CompressionVersion(
                id=f"cmp{index}",
                conversation_id="c1",
                branch_id="b0",
                created_at=T0 + timedelta(minutes=index),
                status=CompressionStatus.PREVIEWED,
                input_message_ids=tuple(f"m{i}" for i in range(index * 50)),
                tokens_before=100000 - index * 1000,
                tokens_after=20000,
                compression_model_id="glm-4.6",
                compression_endpoint_id="relay-b",
                generated_summary=f"第 {index} 次压缩摘要",
            )
            uow.compressions.add(version)
        uow.commit()

    top = factory()
    top.compressions.set_active("b0", "cmp11")
    top.commit()
    top.close()

    _BASELINE["build_seconds"] = time.perf_counter() - started
    return db_path


# ---------- 规模属性 ----------


def test_seeded_at_required_scale(big_db: Path, vault_key: VaultKey) -> None:
    """先确认库真的到了要求的规模——否则后面的性能数字没意义。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    with factory() as uow:
        messages = uow.messages.list_for_branch("b0")
        branches = uow.branches.list_for_conversation("c1")
        compressions = uow.compressions.list_for_branch("b0")
    assert len(branches) == BRANCH_COUNT

    # 主线承载全部 2,000 条；跨分支合计超过 2,000
    assert len(messages) == MESSAGE_COUNT
    total = 0
    with factory() as uow:
        for branch in uow.branches.list_for_conversation("c1"):
            count = len(uow.messages.list_for_branch(branch.id))
            assert count > 0, f"分支 {branch.id} 是空的"
            total += count
    assert total >= MESSAGE_COUNT

    tool_calls = sum(len(m.tool_steps) for m in messages)
    assert tool_calls > 0
    assert len(compressions) == COMPRESSION_VERSIONS


def test_all_tool_calls_round_trip(big_db: Path, vault_key: VaultKey) -> None:
    """500 个工具调用全部能读回（加密往返不丢结构）。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    with factory() as uow:
        messages = uow.messages.list_for_branch("b0")
    tool_calls = [step for m in messages for step in m.tool_steps]
    assert len(tool_calls) == TOOL_CALL_COUNT
    assert all(step.tool_call_id and step.name for step in tool_calls)
    assert all(step.args for step in tool_calls)  # 参数也回来了


# ---------- 性能门槛（宽松但真实） ----------


def test_load_long_conversation_is_linear_enough(big_db: Path, vault_key: VaultKey) -> None:
    """读 2,000 条消息（含逐条解密）应在可用范围内。

    这是本项目特有的成本：字段级加密意味着每条消息都要解密。
    门槛给得宽松（2,000 条 5 秒内），因为断言的是「没有病态行为」，
    不是「某台特定机器上的固定毫秒数」。
    """
    factory = sqlite_uow_factory(big_db, vault_key)
    history = HistoryService(factory)

    started = time.perf_counter()
    opened = history.open_conversation("c1")
    elapsed = time.perf_counter() - started
    _BASELINE["open_conversation_2000_seconds"] = elapsed

    assert opened is not None
    branch_id, messages = opened
    # 必须真的读到主线那 2,000 条——否则测的是别的东西
    assert branch_id == "b0", f"open_conversation 返回了 {branch_id}，不是主线"
    assert len(messages) == MESSAGE_COUNT
    assert elapsed < 5.0, f"读长会话太慢：{elapsed:.2f}s"


def test_conversation_list_scales(big_db: Path, vault_key: VaultKey) -> None:
    """会话列表要在可接受时间内给出（它要扫全部分支的消息计数）。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    started = time.perf_counter()
    summaries = HistoryService(factory).list_conversations()
    elapsed = time.perf_counter() - started
    _BASELINE["list_conversations_seconds"] = elapsed

    assert len(summaries) == 1
    assert summaries[0].message_count >= MESSAGE_COUNT  # 主线 2000 + 分叉各 2 条
    assert elapsed < 5.0, f"会话列表太慢：{elapsed:.2f}s"


def test_search_over_long_history(big_db: Path, vault_key: VaultKey) -> None:
    """在 2,000 条消息上搜索：解密后内存匹配，应在可用范围内。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    started = time.perf_counter()
    hits = HistoryService(factory).search("第 1500 条消息")
    elapsed = time.perf_counter() - started
    _BASELINE["search_2000_seconds"] = elapsed

    assert hits
    assert elapsed < 5.0, f"长历史搜索太慢：{elapsed:.2f}s"


def test_request_logs_scale(big_db: Path, vault_key: VaultKey) -> None:
    """500 条请求日志（含意图 + 传输快照）要能整体读出。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    started = time.perf_counter()
    entries = RequestLogService(factory).list_for_conversation("c1")
    elapsed = time.perf_counter() - started
    _BASELINE["request_logs_500_seconds"] = elapsed

    assert len(entries) == RUN_COUNT
    assert all(e.intent is not None for e in entries)
    assert all(len(e.transports) == 1 for e in entries)
    # 多站点切换：日志里确实出现三个端点
    endpoints = {e.intent.endpoint_id for e in entries if e.intent}
    assert endpoints == {"relay-a", "relay-b", "relay-c"}
    assert elapsed < 5.0, f"请求日志太慢：{elapsed:.2f}s"


def test_compression_versions_and_rollback(big_db: Path, vault_key: VaultKey) -> None:
    """多次压缩与回退：版本历史完整，回退只动启用标记。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    with factory() as uow:
        versions = uow.compressions.list_for_branch("b0")
        active = uow.compressions.get_active("b0")
    assert len(versions) == COMPRESSION_VERSIONS
    assert active is not None and active.id == "cmp11"

    # 回退：清启用标记，版本历史不丢
    with factory() as uow:
        uow.compressions.clear_active("b0")
        uow.commit()
    with factory() as uow:
        assert uow.compressions.get_active("b0") is None
        assert len(uow.compressions.list_for_branch("b0")) == COMPRESSION_VERSIONS


def test_whitelist_survives_scale(big_db: Path, vault_key: VaultKey) -> None:
    """白名单在规模下仍然生效（2000 条里有 8 条被标记）。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    with factory() as uow:
        messages = uow.messages.list_for_branch("b0")
    whitelisted = [m for m in messages if m.is_whitelisted]
    assert whitelisted
    assert all(m.id.startswith("m") for m in whitelisted)


def test_permission_grants_scale(big_db: Path, vault_key: VaultKey) -> None:
    """权限授权在多站点场景下可读（授权边界是加密的）。"""
    factory = sqlite_uow_factory(big_db, vault_key)
    service = PermissionService(factory)
    service.grant("c1", Capability.FILE_READ, allowed_paths=(str(big_db.parent),), note="压测")
    grants = service.list_grants("c1")
    assert len(grants) == 1
    assert grants[0].allowed_paths  # 边界解密回来了


def test_baseline_recorded() -> None:
    """把采集到的基线打印出来（供文档记录），并确认关键项都测到了。"""
    if not _BASELINE:
        pytest.skip("基线在其它用例中采集")
    print("\n=== 长会话性能基线（本机实测）===")
    for key, value in sorted(_BASELINE.items()):
        print(f"{key}: {value:.3f}")
    assert "open_conversation_2000_seconds" in _BASELINE
