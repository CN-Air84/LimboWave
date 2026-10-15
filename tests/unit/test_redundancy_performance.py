"""Avoid duplicate runtime validation during recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from limbowave.application.services.runtime_recovery_coordinator import RuntimeRecoveryCoordinator
from limbowave.domain.runtime_state import (
    RuntimeRestoreResult,
    RuntimeStateSnapshot,
    RuntimeValidationResult,
    fingerprint_from_entry,
)
from limbowave.infrastructure.pi_adapter import PiKernelAdapter


@pytest.mark.parametrize("matching", [True, False])
async def test_pi_recovery_reads_and_validates_entries_only_once(tmp_path, matching):
    entries = [{"id": "e", "type": "message", "message": {"role": "user", "content": "hello"}}]
    snapshot = RuntimeStateSnapshot("c", entries, "e", [fingerprint_from_entry(entries[0], 0)])
    adapter = object.__new__(PiKernelAdapter)
    adapter._session_dir = tmp_path
    adapter._spec = SimpleNamespace(cwd=str(tmp_path))
    adapter._runtime_instance_id = "test"
    adapter._rpc = SimpleNamespace(
        request=AsyncMock(return_value={"success": True, "data": {"cancelled": False}})
    )
    adapter.get_entries = AsyncMock(return_value=entries if matching else [])
    recovery = RuntimeRecoveryCoordinator()
    recovery.attach_replacement_kernel(adapter)
    result = await recovery.recover(snapshot)
    assert result.success is matching
    adapter.get_entries.assert_awaited_once()
    if matching:
        assert result.validation is not None and result.validation.valid
    assert adapter._rpc.request.await_count == 1  # switch_session, never prompt


@pytest.mark.parametrize("provided", [True, False])
@pytest.mark.parametrize("valid", [True, False])
async def test_recovery_reuses_explicit_validation_but_validates_legacy_results(provided, valid):
    snapshot = RuntimeStateSnapshot("c", [{"id": "e"}], "e", [])
    validation = RuntimeValidationResult(valid=valid)
    result = RuntimeRestoreResult(success=True, validation=validation if provided else None)
    kernel = SimpleNamespace(
        restore_runtime_state=AsyncMock(return_value=result),
        validate_runtime_state=AsyncMock(return_value=validation),
        runtime_instance_id=lambda: "test",
    )
    recovery = RuntimeRecoveryCoordinator()
    recovery.attach_replacement_kernel(kernel)
    recovered = await recovery.recover(snapshot)
    assert recovered.success is valid
    assert recovered.validation is validation
    assert kernel.validate_runtime_state.await_count == (0 if provided else 1)
