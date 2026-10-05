"""Real Pi processes + SQLite: forked requests remain compacted after a cold restart."""

import asyncio
import json
import shutil

import pytest

from limbowave.application.history_payload import history_payload
from limbowave.application.kernel import KernelSetup
from limbowave.application.services.compression_service import CompressionService
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.composition import build_kernel
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from tests.integration.test_compression_runtime import requests
from tests.integration.test_recovery_e2e import _paths, _seed, _send_and_wait

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="requires Pi")


@pytest.mark.parametrize("action", ["fork", "regenerate", "edit"])
async def test_real_branch_compression_survives_cold_restart(
    tmp_path,
    mock_provider,
    vault_key,
    action,
):
    port, log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)
    database = paths.data_root / "history.db"

    def open_runtime():
        setup = build_kernel(paths, vault_key)
        assert isinstance(setup, KernelSetup)
        factory = sqlite_uow_factory(database, vault_key)
        context = RunContext(
            logical_model_id=setup.logical_model_id,
            endpoint_id=setup.endpoint_id,
            routing_reason=setup.routing_reason,
            app_params=dict(setup.app_params),
        )
        return RunCoordinator(setup.kernel, factory, context=lambda: context), factory

    coord, factory = open_runtime()
    try:
        await coord.start()
        await _send_and_wait(coord, "COVERED-CANARY " + "old text " * 1000)
        await coord.wait_idle()
        service = CompressionService(factory)
        version = service.create_version(
            coord.conversation_id,
            coord.branch_id,
            tokens_before=12000,
            compression_model_id="mock-model",
            compression_endpoint_id="mock",
        )
        service.record_result(version.id, "DURABLE-BRANCH-SUMMARY", 10)
        assert await coord.apply_compression(version.id)
        await _send_and_wait(coord, "KEPT-TAIL")
        await coord.wait_idle()
        conversation, parent = coord.conversation_id, coord.branch_id
        # Resume appends the overlay after the tail. Native fork alone drops it.
        assert await coord.resume(conversation, parent)
        messages = HistoryService(factory).branch_messages(parent)
        if action == "fork":
            assert await coord.fork_message(messages[-1].id)
            await _send_and_wait(coord, "AFTER-FORK")
        elif action == "regenerate":
            assert await coord.regenerate(messages[-1].id)
        else:
            assert await coord.edit_user_message(messages[-2].id, "EDITED-TAIL")
        async with asyncio.timeout(60):
            while coord.busy:
                await asyncio.sleep(0.05)
        await coord.wait_idle()
        child = coord.branch_id
        body = json.dumps(requests(log)[-1]["body"])
        assert "DURABLE-BRANCH-SUMMARY" in body and "COVERED-CANARY" not in body
        assert service.get_active(child) is not None
    finally:
        await coord.shutdown()
        factory.close()

    coord, factory = open_runtime()
    try:
        await coord.start()
        assert await coord.resume(conversation, child)
        messages = HistoryService(factory).branch_messages(child)
        assert any(
            marker.is_active
            for entry in history_payload(messages, uow_factory=factory, branch_id=child)
            for marker in entry.compressions
        )
        await _send_and_wait(coord, "AFTER-COLD-RESTART")
        await coord.wait_idle()
        body = json.dumps(requests(log)[-1]["body"])
        assert "DURABLE-BRANCH-SUMMARY" in body
        assert "COVERED-CANARY" not in body
        assert ("EDITED-TAIL" if action == "edit" else "KEPT-TAIL") in body
        assert await coord.rollback_compression(child)
        await _send_and_wait(coord, "AFTER-CHILD-ROLLBACK")
        await coord.wait_idle()
        body = json.dumps(requests(log)[-1]["body"])
        assert "COVERED-CANARY" in body and "DURABLE-BRANCH-SUMMARY" not in body
        assert CompressionService(factory).get_active(parent) is not None
    finally:
        await coord.shutdown()
        factory.close()
