"""Runtime capability reads must not guess from a model name."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from limbowave.infrastructure.pi_adapter import PiKernelAdapter
from limbowave.infrastructure.pi_rpc import SpawnSpec


@pytest.mark.parametrize("levels", [["off"], ["off", "low", "high", "max"]])
async def test_reads_runtime_thinking_levels(levels):
    adapter = PiKernelAdapter(SpawnSpec(argv=["unused"]))
    request = AsyncMock(return_value={"success": True, "data": {"levels": levels}})
    adapter._rpc = SimpleNamespace(request=request)
    assert await adapter.get_available_thinking_levels() == tuple(levels)
    request.assert_awaited_once_with({"type": "get_available_thinking_levels"})


@pytest.mark.parametrize("response", [
    {"success": False}, {"success": True, "data": {}},
    {"success": True, "data": {"levels": "all"}},
])
async def test_rejects_missing_or_invalid_runtime_capabilities(response):
    adapter = PiKernelAdapter(SpawnSpec(argv=["unused"]))
    adapter._rpc = SimpleNamespace(request=AsyncMock(return_value=response))
    with pytest.raises(RuntimeError):
        await adapter.get_available_thinking_levels()
