"""运行/Pi 埋点的隐私与行为边界。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.infrastructure.diagnostics import DiagnosticRuntime, LogConfig, LogReader
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter
from limbowave.infrastructure.pi_rpc import PiRpcProcess, SpawnSpec
from tests.unit.test_run_coordinator import FakeKernel


@pytest.mark.asyncio
async def test_run_lifecycle_has_ids_without_content_or_delta_flood(tmp_path: Path) -> None:
    with DiagnosticRuntime(LogConfig(tmp_path, level="debug", disk_bytes_per_second=0)) as runtime:
        kernel = FakeKernel()
        coordinator = RunCoordinator(
            kernel,
            in_memory_uow_factory(),
            context=lambda: RunContext(
                "fake",
                "fake",
                "test",
                app_params={"model": "fake"},
            ),
        )
        run_id = await coordinator.send("PRIVATE_USER_MESSAGE")
        assert run_id is not None
        before = runtime.manager.status().sequence
        for _ in range(500):
            coordinator._emit("assistant_delta", text="PRIVATE_STREAM_CONTENT")
            coordinator._emit("thinking_delta", text="PRIVATE_THINKING_CONTENT")
        assert runtime.manager.status().sequence == before
        kernel.say("PRIVATE_ASSISTANT_MESSAGE")
        kernel.emit("run.settled", {})
        await coordinator.wait_idle()
        assert runtime.manager.flush()
        result = runtime.manager.recent()
        starts = [entry for entry in result if entry.message == "run.started"]
        ends = [entry for entry in result if entry.message == "run.finalized"]
        assert len(starts) == len(ends) == 1
        assert starts[0].context["run_id"] == ends[0].context["run_id"] == run_id
        assert ends[0].context["status"] == "completed"
        assert starts[0].context["conversation_id"]
        assert "PRIVATE_" not in repr(result)
        assert "PRIVATE_" not in (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8")


def test_structured_stderr_is_not_copied_to_debug_log(tmp_path: Path) -> None:
    with DiagnosticRuntime(LogConfig(tmp_path, level="debug")) as runtime:
        adapter = PiKernelAdapter(SpawnSpec([sys.executable], cwd=str(tmp_path)))
        observed = []
        adapter.set_observation_handler(observed.append)
        payload = {
            "kind": "provider.request",
            "model": "fake",
            "payload": {
                "messages": [{"role": "user", "content": "PRIVATE_REQUEST_BODY"}],
                "headers": {"Authorization": "arbitrary-private-credential"},
            },
        }
        adapter._rpc._handle_stderr_frame(("[LIMBOWAVE] " + json.dumps(payload) + "\n").encode())
        adapter._rpc._handle_stderr_frame(b"plain warning password=ordinary-secret\n")
        assert runtime.manager.flush()
        result = list(LogReader(runtime.manager.config).iter_entries())
        assert observed == [payload]
        assert any(entry.context.get("observation_kind") == "provider.request" for entry in result)
        assert "PRIVATE_REQUEST_BODY" not in repr(result)
        assert "arbitrary-private-credential" not in repr(result)
        assert "ordinary-secret" not in repr(result)


def test_stderr_cache_is_bounded_but_observation_frames_are_complete() -> None:
    received = []
    rpc = PiRpcProcess(SpawnSpec([sys.executable], stderr_handler=received.append))
    for index in range(4):
        frame = "[LIMBOWAVE] " + str(index) + "x" * 200_000 + "\n"
        rpc._handle_stderr_frame(frame.encode())
    assert len(received) == 4
    assert all(len(frame) > 200_000 for frame in received)
    assert len(rpc.stderr_text) <= 256 * 1024
    assert rpc.stderr_text.endswith(received[-1])


@pytest.mark.asyncio
async def test_rpc_timeout_and_cancellation_clean_pending_without_recording_command(
    tmp_path: Path, monkeypatch
) -> None:
    with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
        rpc = PiRpcProcess(SpawnSpec([sys.executable]))
        monkeypatch.setattr(rpc, "send", AsyncMock(return_value="fake"))
        with pytest.raises(TimeoutError):
            await rpc.request({"type": "prompt", "message": "PRIVATE_PROMPT"}, timeout=0.001)
        assert not rpc._pending
        task = asyncio.create_task(rpc.request({"type": "prompt", "message": "PRIVATE_PROMPT"}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not rpc._pending
        assert runtime.manager.flush()
        records = runtime.manager.recent()
        assert records[0].message == "pi.command_timeout"
        assert "PRIVATE_PROMPT" not in repr(records)


def test_malformed_protocol_and_observation_do_not_dump_raw_frames(tmp_path: Path) -> None:
    with DiagnosticRuntime(LogConfig(tmp_path, level="debug")) as runtime:
        adapter = PiKernelAdapter(SpawnSpec([sys.executable], cwd=str(tmp_path)))
        adapter._rpc._dispatch(b"PRIVATE_BROKEN_FRAME")
        adapter._rpc._dispatch(b"[]")
        adapter._rpc._handle_stderr_frame(b"[LIMBOWAVE] PRIVATE_BROKEN_OBSERVATION\n")
        assert runtime.manager.flush()
        records = runtime.manager.recent()
        assert sum(entry.message == "pi.invalid_frame" for entry in records) == 2
        assert any(entry.message == "pi.invalid_observation" for entry in records)
        assert "PRIVATE_" not in repr(records)
