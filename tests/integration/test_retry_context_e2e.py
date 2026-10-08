"""Retries must replace a turn in real Pi context, not only in the GUI projection."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from limbowave.application import branch_path
from limbowave.application.history_payload import history_payload
from limbowave.application.services.attachment_service import AttachmentPayload, AttachmentService
from limbowave.application.services.file_service import FileService
from limbowave.application.services.image_service import ImageService
from limbowave.application.services.run_coordinator import (
    RunContext,
    RunCoordinator,
    _compose_prompt,
)
from limbowave.composition import build_kernel
from limbowave.domain.retry import RetryPolicy
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.integration.test_run_coordinator_e2e import _paths, _read_jsonl, _seed

pytestmark = pytest.mark.skipif(__import__("shutil").which("node") is None, reason="requires Pi")


async def _settle(coordinator: RunCoordinator) -> None:
    async with asyncio.timeout(60):
        while coordinator.busy:
            await asyncio.sleep(.05)
        await coordinator.wait_idle()


def _user_texts(body: dict) -> list[str]:
    texts = [
        message["content"] if isinstance(message["content"], str)
        else "".join(block.get("text", "") for block in message["content"])
        for message in body["messages"] if message["role"] == "user"
    ]
    # Pi maps extension custom messages to role=user. The ephemeral document
    # manifest is checked separately, and is not an application user turn.
    return [text for text in texts if not text.startswith("当前分支持久附件清单（")]


@pytest.mark.parametrize("with_document", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("prior_turn", [False, True])
async def test_retry_request_context_has_one_copy_of_turn(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey,
    automatic: bool, prior_turn: bool, with_document: bool,
) -> None:
    port, log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)
    setup = build_kernel(paths, vault_key)
    assert setup is not None
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    coordinator = RunCoordinator(setup.kernel, factory, context=lambda: RunContext(
        logical_model_id=setup.logical_model_id, endpoint_id=setup.endpoint_id,
        routing_reason=setup.routing_reason, app_params=dict(setup.app_params),
        retry_policy=RetryPolicy(max_attempts=3 if automatic else 1, base_delay_ms=0),
    ))
    payload = AttachmentPayload()
    if with_document:
        files = FileService(factory)
        path = tmp_path / "reference.md"
        path.write_text("document body must not be inlined", encoding="utf-8")
        document = files.index_path(path)
        attachments = AttachmentService(
            files, ImageService(factory, BlobStore(tmp_path, vault_key)),
        )
        coordinator.attachment_builder = attachments.build
        payload = attachments.build([document.id])
    async def send():
        return await coordinator.send(
            "same input", attachment_ids=payload.attachment_ids,
            document_note=payload.document_note,
        )
    try:
        await coordinator.start()
        if prior_turn:
            await send()
            await _settle(coordinator)
        start = len(_read_jsonl(log))
        async with httpx.AsyncClient() as client:
            response = await client.put(f"http://127.0.0.1:{port}/control/fail", json={
                "count": 2, "status": 503,
                "message": "ECONNRESET connection reset" if automatic else "injected failure",
            })
            response.raise_for_status()
        run_id = await send()
        await _settle(coordinator)
        branch_id = coordinator.branch_id
        if not automatic:
            for _ in range(2):
                assert store.runs[run_id].status is RunStatus.FAILED
                # Also exercise retry after rebuilding runtime from persisted mirrors.
                assert await coordinator.switch_conversation(coordinator.conversation_id, branch_id)
                run_id = await coordinator.retry_user_message(store.runs[run_id].user_message_id)
                assert run_id is not None
                await _settle(coordinator)
        assert store.runs[run_id].status is RunStatus.COMPLETED
        assert coordinator.branch_id == branch_id
        received = _read_jsonl(log)[start:]
        assert [request["http_status"] for request in received] == [503, 503, 200]
        expected = [_compose_prompt("same input", payload.document_note)] * (2 if prior_turn else 1)
        assert [_user_texts(request["body"]) for request in received] == [expected] * 3
        if with_document:
            for request in received:
                messages_json = json.dumps(request["body"]["messages"], ensure_ascii=False)
                assert document.id in messages_json
                assert messages_json.count("当前分支持久附件清单") == 1
        with factory() as uow:
            messages = branch_path.branch_messages(uow, branch_id)
        visible = history_payload(messages, uow_factory=factory, branch_id=branch_id)
        assert [entry.content for entry in visible if entry.role == "user"] == (
            ["same input"] * len(expected)
        )
        assert len(store.runs) == (1 if automatic else 3) + int(prior_turn)
        # Persisted entry parents must not resurrect the discarded attempts on reopen.
        assert await coordinator.switch_conversation(coordinator.conversation_id, branch_id)
        await coordinator.send("follow up")
        await _settle(coordinator)
        assert _user_texts(_read_jsonl(log)[-1]["body"]) == [*expected, "follow up"]
    finally:
        await coordinator.shutdown()
