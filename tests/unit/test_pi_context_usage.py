"""Post-compaction usage must be readable without another provider request."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from limbowave.application.kernel import ContextUsage
from limbowave.application.services.compression_service import CompressionService, ThresholdAction
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter
from limbowave.infrastructure.pi_rpc import SpawnSpec


def adapter_with_responses(*responses):
    adapter = PiKernelAdapter(SpawnSpec(argv=["unused"]))
    rpc = AsyncMock(side_effect=responses)
    adapter._rpc = SimpleNamespace(request=rpc)
    return adapter, rpc


def unknown_usage(window=100):
    return {"data": {"contextUsage": {"tokens": None, "percent": None, "contextWindow": window}}}


async def test_post_compaction_estimates_summary_and_tail_without_stale_usage():
    adapter, rpc = adapter_with_responses(
        unknown_usage(),
        {
            "data": {
                "messages": [
                    {"role": "compactionSummary", "summary": "s" * 16},
                    {"role": "user", "content": "t" * 12},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "a" * 8}],
                        "usage": {"input": 9500, "totalTokens": 9600},
                    },
                ]
            }
        },
    )
    service = CompressionService(in_memory_uow_factory(InMemoryStore()))
    report = await service.estimate(adapter)
    assert report.usage == ContextUsage(tokens=9, context_window=100, percent=9.0)
    assert report.action is ThresholdAction.NONE
    assert report.display == "约 9%（9/100 tokens，估算）"
    assert [call.args[0] for call in rpc.call_args_list] == [
        {"type": "get_session_stats"},
        {"type": "get_messages"},
    ]


@pytest.mark.parametrize("tokens,percent", [(42, 42.0), (0, 0.0)])
async def test_valid_native_usage_does_not_fetch_messages(tokens, percent):
    adapter, rpc = adapter_with_responses(
        {"data": {"contextUsage": {"tokens": tokens, "percent": percent, "contextWindow": 100}}},
    )
    assert await adapter.get_context_usage() == ContextUsage(tokens, 100, percent)
    rpc.assert_awaited_once_with({"type": "get_session_stats"})


@pytest.mark.parametrize("data", [{}, {"contextUsage": None}, {"contextUsage": {}}])
async def test_missing_native_capability_remains_unknown(data):
    adapter, rpc = adapter_with_responses({"data": data})
    assert await adapter.get_context_usage() is None
    assert rpc.await_count == 1


@pytest.mark.parametrize("window", [None, 0, -1])
async def test_unknown_window_does_not_invent_a_percentage(window):
    adapter, rpc = adapter_with_responses(unknown_usage(window))
    assert await adapter.get_context_usage() is None
    assert rpc.await_count == 1


@pytest.mark.parametrize("response", [{}, {"data": {}}, {"data": {"messages": None}}])
async def test_missing_effective_messages_remain_unknown(response):
    adapter, _ = adapter_with_responses(unknown_usage(), response)
    assert await adapter.get_context_usage() is None


async def test_empty_effective_context_is_zero_not_unknown():
    adapter, _ = adapter_with_responses(unknown_usage(), {"data": {"messages": []}})
    assert await adapter.get_context_usage() == ContextUsage(0, 100, 0.0)


async def test_estimates_are_not_cached_and_next_native_usage_takes_priority():
    adapter, rpc = adapter_with_responses(
        unknown_usage(),
        {"data": {"messages": [{"role": "compactionSummary", "summary": "abcd"}]}},
        unknown_usage(200),
        {"data": {"messages": [{"role": "compactionSummary", "summary": "x" * 16}]}},
        {"data": {"contextUsage": {"tokens": 60, "percent": 30.0, "contextWindow": 200}}},
    )
    assert await adapter.get_context_usage() == ContextUsage(1, 100, 1.0)
    assert await adapter.get_context_usage() == ContextUsage(4, 200, 2.0)
    assert await adapter.get_context_usage() == ContextUsage(60, 200, 30.0)
    assert rpc.await_count == 5


@pytest.mark.parametrize(
    "message,expected",
    [
        ({"role": "user", "content": "abcde"}, 2),
        ({"role": "user", "content": "压😀😀"}, 2),
        ({"role": "custom", "content": "abcde", "details": {"internal": "x" * 10000}}, 2),
        (
            {
                "role": "toolResult",
                "content": [
                    {"type": "text", "text": "1234"},
                    {"type": "image", "data": "x" * 10000},
                ],
            },
            1201,
        ),
        (
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "1234", "thinkingSignature": "x" * 10000},
                    {"type": "text", "text": "1234"},
                    {"type": "toolCall", "name": "read", "arguments": {"path": "文档"}},
                ],
            },
            7,
        ),
        ({"role": "bashExecution", "command": "pwd", "output": "abcde"}, 2),
        ({"role": "branchSummary", "summary": "abcde"}, 2),
        ({"role": "compactionSummary", "summary": "abcde"}, 2),
    ],
)
def test_message_estimates_match_pi_content_heuristics(message, expected):
    from limbowave.infrastructure.pi_runtime.context_usage import estimate_message_tokens

    assert estimate_message_tokens(message) == expected
