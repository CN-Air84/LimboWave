"""Real Pi + local mock provider: assert the actual next request is compacted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.application.kernel import KernelSetup
from limbowave.application.services.compression_service import CompressionService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.composition import build_kernel
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.integration.test_recovery_e2e import _paths, _seed, _send_and_wait

pytestmark = pytest.mark.skipif(__import__("shutil").which("node") is None, reason="requires Pi")


def requests(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def test_real_compaction_isolation_tail_resume_and_rollback(
    tmp_path: Path,
    mock_provider: tuple[int, Path],
    vault_key: VaultKey,
):
    port, log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)
    setup = build_kernel(paths, vault_key)
    assert isinstance(setup, KernelSetup)
    kernel = setup.kernel
    factory = in_memory_uow_factory(InMemoryStore())
    context = RunContext(
        logical_model_id=setup.logical_model_id,
        endpoint_id=setup.endpoint_id,
        routing_reason=setup.routing_reason,
        app_params=dict(setup.app_params),
    )
    coord = RunCoordinator(kernel, factory, context=lambda: context)
    service = CompressionService(factory)
    try:
        await coord.start()
        await _send_and_wait(coord, "OLD-CANARY-DO-NOT-REPLAY " + "x" * 10000)
        await coord.wait_idle()
        conversation, branch = coord.conversation_id, coord.branch_id
        version = service.create_version(
            conversation,
            branch,
            tokens_before=42000,
            compression_model_id=setup.logical_model_id,
            compression_endpoint_id=setup.endpoint_id,
        )
        original = await kernel.get_entries()
        assert await service.generate_isolated(version.id, kernel, timeout=60)
        assert await kernel.get_entries() == original, "Preview changed the live Pi tree"
        await _send_and_wait(coord, "TAIL-AFTER-PREVIEW")
        await coord.wait_idle()
        before = await kernel.get_context_usage()
        assert before is not None
        count = len(requests(log))
        assert await coord.apply_compression(version.id, edited_summary="SHORT-ACCEPTED-SUMMARY")
        assert len(requests(log)) == count, "Applying a preview must not call the provider again"
        report = await service.estimate(kernel)
        assert report.usage is not None
        assert 0 < report.usage.tokens < before.tokens
        assert "估算" in report.display and "未知" not in report.display
        assert len(requests(log)) == count, "Usage refresh must not call the provider"
        assert await coord.new_session()
        assert await coord.resume(conversation, branch)
        assert await kernel.get_context_usage() == report.usage
        assert len(requests(log)) == count, "Restored usage must not call the provider"
        await _send_and_wait(coord, "AFTER-ACCEPT")
        await coord.wait_idle()
        body = json.dumps(requests(log)[-1]["body"], ensure_ascii=False)
        assert "SHORT-ACCEPTED-SUMMARY" in body
        assert "TAIL-AFTER-PREVIEW" in body and "AFTER-ACCEPT" in body
        assert "OLD-CANARY-DO-NOT-REPLAY" not in body
        # Continue after another resume: stable mirror IDs must not reconnect to an old leaf.
        assert await coord.resume(conversation, branch)
        await _send_and_wait(coord, "AFTER-SECOND-RESUME")
        await coord.wait_idle()
        body = json.dumps(requests(log)[-1]["body"], ensure_ascii=False)
        assert "TAIL-AFTER-PREVIEW" in body and "AFTER-ACCEPT" in body
        assert "OLD-CANARY-DO-NOT-REPLAY" not in body
        # The mock does not emit usage; the content fallback must keep working.
        continued_usage = await kernel.get_context_usage()
        assert continued_usage is not None and continued_usage.tokens > report.usage.tokens
        assert await coord.rollback_compression(branch)
        await _send_and_wait(coord, "AFTER-ROLLBACK")
        await coord.wait_idle()
        body = json.dumps(requests(log)[-1]["body"], ensure_ascii=False)
        assert "OLD-CANARY-DO-NOT-REPLAY" in body
        assert "TAIL-AFTER-PREVIEW" in body and "AFTER-ACCEPT" in body
        assert "AFTER-SECOND-RESUME" in body
        assert "SHORT-ACCEPTED-SUMMARY" not in body
    finally:
        await coord.shutdown()
