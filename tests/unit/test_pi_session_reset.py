"""Pi 的 success 只表示命令已处理，cancelled 与恢复内容必须另外核验。"""

from unittest.mock import AsyncMock

import pytest

from limbowave.application.kernel import KernelState
from limbowave.domain.runtime_state import (
    RestoreFailureReason,
    RuntimeStateSnapshot,
    fingerprint_from_entry,
)
from limbowave.infrastructure.pi_adapter import PiKernelAdapter
from limbowave.infrastructure.pi_rpc import SpawnSpec


def _adapter(tmp_path):
    return PiKernelAdapter(SpawnSpec(argv=["unused"], cwd=str(tmp_path)))


@pytest.mark.parametrize("response", [
    {"success": False, "error": "failed"},
    {"success": True, "data": {"cancelled": True}},
    {"success": True},
])
async def test_reset_requires_explicit_confirmation(tmp_path, response):
    adapter = _adapter(tmp_path)
    adapter._rpc.request = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match="未确认"):
        await adapter.new_session()
    assert adapter._rpc.request.await_count == 1


async def test_successful_reset_with_remaining_context_is_rejected(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._rpc.request = AsyncMock(return_value={"success": True, "data": {"cancelled": False}})
    adapter.get_state = AsyncMock(return_value=KernelState(
        model_id="m", thinking_level="off", is_streaming=False, is_compacting=False,
        session_id="new", session_name=None, message_count=0, pending_message_count=0,
    ))
    adapter.get_entries = AsyncMock(return_value=[{"type": "compaction", "summary": "old"}])
    with pytest.raises(RuntimeError, match="仍含历史"):
        await adapter.new_session()


@pytest.mark.parametrize("cancelled", [True, False])
async def test_restore_cancellation_and_wrong_entries_are_failures(tmp_path, cancelled):
    adapter = _adapter(tmp_path)
    entry = {"id": "target", "type": "message", "message": {"role": "user", "content": "target"}}
    snapshot = RuntimeStateSnapshot(
        "c", [entry], "target", [fingerprint_from_entry(entry, branch_position=0)]
    )
    adapter._rpc.request = AsyncMock(
        return_value={"success": True, "data": {"cancelled": cancelled}}
    )
    adapter.get_entries = AsyncMock(return_value=[])
    result = await adapter.restore_runtime_state(snapshot)
    assert not result.success
    assert result.failure_reason == (
        RestoreFailureReason.SESSION_SWITCH_FAILED if cancelled
        else RestoreFailureReason.ENTRY_VALIDATION_FAILED
    )
    if cancelled:
        assert not list(adapter._session_dir.glob("*.jsonl"))
