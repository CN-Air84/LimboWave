"""真实 Pi 重建运行时后，mock 收到的模型和图片必须仍匹配应用意图。"""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from tests.integration.test_pi_adapter import mock_models as _mock_models

route_models = _mock_models

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="


def test_original_image_fixture_has_valid_png_checksums() -> None:
    # Only the old fixture's broken IDAT CRC changed; pixel bytes are unchanged.
    with Image.open(BytesIO(base64.b64decode(PNG, validate=True))) as image:
        assert image.format == "PNG"
        assert image.size == (1, 1)
        image.verify()


def assert_original_image_for_turn(body: dict, user_text: str) -> None:
    # Application custom context can become another wire-level user message.
    # Match this turn's exact text, never the last user role or any historical image.
    matching = [
        message for message in body["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        and "".join(part["text"] for part in message["content"] if part["type"] == "text")
        == user_text
    ]
    assert len(matching) == 1, f"Expected exactly one user message for {user_text!r}"
    sent_images = [part for part in matching[0]["content"] if part["type"] == "image_url"]
    # Exact data URL equality preserves every image byte, not just an image count.
    assert sent_images == [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}
    ]


async def settled(coord: RunCoordinator) -> None:
    async with asyncio.timeout(40):
        while coord.busy:
            await asyncio.sleep(0.05)
        await coord.wait_idle()


@pytest.mark.parametrize("replacement", ["fork", "restore"])
async def test_replacement_sends_selected_model_and_original_images(
    replacement: str,
    route_models: Path,
    mock_provider: tuple[int, Path],
    tmp_path: Path,
) -> None:
    _, log = mock_provider
    models_path = route_models / ".pi" / "agent" / "models.json"
    models = json.loads(models_path.read_text(encoding="utf-8"))
    old_provider = models["providers"]["mock"]
    old_provider["models"][0]["input"] = ["text", "image"]
    models["providers"]["selected-relay"] = {
        **old_provider,
        "models": [{**old_provider["models"][0], "id": "selected-flash"}],
    }
    models_path.write_text(json.dumps(models), encoding="utf-8")
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    store = InMemoryStore()
    selected = RunContext(
        logical_model_id="old",
        endpoint_id="mock",
        routing_reason="initial",
        app_params={"model": "mock-model"},
        supports_images=True,
        thinking_level="off",
    )
    coord = RunCoordinator(adapter, in_memory_uow_factory(store), context=lambda: selected)
    images = [{"type": "image", "data": PNG, "mimeType": "image/png"}]
    try:
        await coord.start()
        first = await coord.send("original", attachment_ids=["image"], images=images)
        await settled(coord)
        assert store.runs[first].status is RunStatus.COMPLETED, store.runs[first].error
        first_wire = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert_original_image_for_turn(first_wire[-1]["body"], "original")
        snapshot = await adapter.export_runtime_state()
        selected = RunContext(
            logical_model_id="flash",
            endpoint_id="selected-relay",
            routing_reason="selected",
            app_params={"model": "selected-flash"},
            supports_images=True,
            thinking_level="off",
        )
        await adapter.set_model("selected-relay", "selected-flash")
        if replacement == "fork":
            run = await coord.edit_user_message(
                store.runs[first].user_message_id,
                "edited",
                attachment_ids=["image"],
                images=images,
            )
        else:
            result = await adapter.restore_runtime_state(snapshot)
            assert result.success, result.error
            run = await coord.send("after restore", attachment_ids=["image"], images=images)
        await settled(coord)
        assert store.runs[run].status is RunStatus.COMPLETED, store.runs[run].error
        wire = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        body = wire[-1]["body"]
        assert body["model"] == "selected-flash"
        assert len(wire) > len(first_wire), "Replacement must produce a new provider request"
        expected_text = "edited" if replacement == "fork" else "after restore"
        assert_original_image_for_turn(body, expected_text)
        transport = next(t for t in store.transports.values() if t.run_id == run)
        assert transport.provider == "selected-relay"
        assert transport.model_id == "selected-flash"
        assert transport.body == body
    finally:
        await coord.shutdown()
