"""The chat context ring refreshes immediately after compaction, without a turn."""

from pytestqt.qtbot import QtBot

from limbowave.application.services.compression_service import CompressionService
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.chat_view import ChatView
from tests.unit.test_pi_context_usage import adapter_with_responses, unknown_usage


async def test_chat_ring_uses_post_compaction_estimate_without_another_turn(qtbot: QtBot):
    adapter, rpc = adapter_with_responses(
        unknown_usage(),
        {"data": {"messages": [{"role": "compactionSummary", "summary": "x" * 80}]}},
    )
    service = CompressionService(in_memory_uow_factory(InMemoryStore()))
    chat = ChatView()
    qtbot.addWidget(chat)
    chat.set_context_usage(95.0, "约 95%（压缩前）")

    report = await service.estimate(adapter)
    chat.set_context_usage(report.usage.percent if report.usage else None, report.display)
    chat.set_usage_action(report.action.value)

    assert chat._usage_bar.percent == 20.0
    assert chat._usage_bar.toolTip() == "约 20%（20/100 tokens，估算）"
    assert "未知" not in chat._usage_bar.toolTip()
    assert [call.args[0]["type"] for call in rpc.call_args_list] == [
        "get_session_stats", "get_messages",
    ]
