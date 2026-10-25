"""Real Pi image normalization must not break application branch anchors."""

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from limbowave.application.services.attachment_service import AttachmentPayload
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.pi_rpc import resolve_pi_argv
from tests.integration.test_pi_adapter import mock_models as mock_models


@pytest.fixture
def pi_cli():
    try:
        return Path(resolve_pi_argv()[1])
    except RuntimeError:
        # The developer's Pi launcher can use a managed release instead of npm.
        root = Path.home() / ".pi" / "agent" / "install"
        current = root / "current-version"
        if current.is_file():
            version = current.read_text(encoding="utf-8").strip()
            if version and Path(version).name == version:
                candidate = (
                    root
                    / "releases"
                    / version
                    / "node_modules"
                    / "@earendil-works"
                    / "pi-coding-agent"
                    / "dist"
                    / "bundle"
                    / "cli.js"
                )
                if candidate.is_file():
                    return candidate
        pytest.skip("Pi CLI is not installed")


@pytest.mark.parametrize("prompt", ["Describe the image", ""])
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_image_hint_can_fork_and_regenerate(
    pi_cli, mock_models, mock_provider, tmp_path, vault_key, legacy, prompt
):
    models = mock_models / ".pi" / "agent" / "models.json"
    config = json.loads(models.read_text(encoding="utf-8"))
    config["providers"]["mock"]["models"][0]["input"] = ["text", "image"]
    models.write_text(json.dumps(config), encoding="utf-8")
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path, cli_path=pi_cli)
    kernel = PiKernelAdapter(spec)
    factory = sqlite_uow_factory(tmp_path / "history.db", vault_key)
    coord = RunCoordinator(
        kernel,
        factory,
        context=lambda: RunContext("mock-model", "mock", "test", supports_images=True),
    )
    image = io.BytesIO()
    Image.new("RGB", (4096, 128), "white").save(image, format="PNG")
    images = [
        {
            "type": "image",
            "mimeType": "image/png",
            "data": base64.b64encode(image.getvalue()).decode(),
        }
    ]

    coord.attachment_builder = lambda ids: AttachmentPayload(
        attachment_ids=list(ids), images=images
    )

    async def settled():
        async with asyncio.timeout(60):
            while coord.busy:
                await asyncio.sleep(0.05)
            await coord.wait_idle()

    try:
        await coord.start()
        assert await coord.send(prompt, attachment_ids=["test-image"], images=images)
        await settled()
        branch = coord.branch_id
        original = HistoryService(factory).branch_messages(branch)
        assert len(original) == 2
        with factory() as uow:
            user = next(
                m
                for m in uow.runtime.list_for_conversation(coord.conversation_id)
                if m.payload.get("message", {}).get("role") == "user"
            )
            text = coord._extract_text(user.payload["message"])
            assert text != prompt, "Test must exercise Pi's appended image hints"
            assert user.message_id == original[0].id
        if legacy:
            # Preserve the real timestamp/normalized text, clear only the old link.
            from dataclasses import replace

            from tests.unit.test_runtime_message_anchors import replace_mirrors

            with factory() as uow:
                replace_mirrors(uow, [replace(user, message_id=None)])
                uow.commit()
        assert await coord.fork_message(original[-1].id)
        assert await coord.regenerate(original[-1].id)
        await settled()
        assert HistoryService(factory).branch_messages(branch) == original
        assert len(HistoryService(factory).branch_messages(coord.branch_id)) == 2
    finally:
        await coord.shutdown()
        factory.close()
