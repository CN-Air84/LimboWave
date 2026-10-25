"""Real attachment imports must retain full resolution for preview, not a 256px copy."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea

from limbowave import app
from limbowave.application.services.image_service import ImageService
from limbowave.bootstrap import AppPaths
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot
from limbowave.infrastructure import shell
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.ui.sent_attachments import ImagePreviewDialog, SentAttachment
from tests.ui.test_attachment_bar import _make_png_bytes
from tests.ui.test_history_loading import _until


@pytest.fixture
async def attachment_app(qtbot, qapp, monkeypatch, tmp_path, vault_key):
    from limbowave.ui.main_window import MainWindow

    stylesheet = qapp.styleSheet()
    monkeypatch.setattr(app, "_build_kernel", lambda *_a, **_kw: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)

    class LocalIpc:
        session = rate_limit_session = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    def finish_bootstrap(coro):
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("Unexpected asynchronous work in IPC startup stub")

    monkeypatch.setattr(app, "ToolIpcServer", LocalIpc)
    window = MainWindow()
    qtbot.addWidget(window)
    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(asyncio.get_running_loop(), "run_until_complete", finish_bootstrap)
        controller, _ipc, _warm, _startup, shutdown = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), vault_key
        )
    try:
        images = ImageService(
            controller.coordinator()._uow_factory, BlobStore(tmp_path / "blobs", vault_key)
        )
        yield window, controller, images
    finally:
        await shutdown()
        qapp.setStyleSheet(stylesheet)


@pytest.mark.parametrize("size", [(1600, 900), (900, 1600), (80, 60)])
@pytest.mark.parametrize("pasted", [False, True])
async def test_imported_preview_keeps_original_resolution(
    attachment_app, qtbot, tmp_path, size, pasted
):
    raw = _make_png_bytes(*size)
    window, controller, images = attachment_app
    chat = window.chat
    if pasted:
        chat.image_pasted.emit(raw)
    else:
        path = tmp_path / "original.png"
        path.write_bytes(raw)
        chat.files_dropped.emit([str(path)])
    await _until(lambda: bool(chat.attachments.attachment_ids()))
    attachment_id = chat.attachments.attachment_ids()[0]
    draft = chat.attachments._chips[attachment_id].draft
    assert draft.thumbnail is not None
    assert draft.thumbnail.size() == QSize(*size)
    assert draft.thumbnail.toImage() == QImage.fromData(raw)
    assert images.read_bytes(attachment_id) == raw
    payload = controller.coordinator().attachment_builder([attachment_id])
    assert base64.b64decode(payload.images[0]["data"]) == raw

    thumbnails = [
        label.pixmap()
        for label in chat.attachments._chips[attachment_id].findChildren(QLabel)
        if not label.pixmap().isNull()
    ]
    assert len(thumbnails) == 1
    assert max(thumbnails[0].width(), thumbnails[0].height()) <= 40

    _assert_original_preview(
        qtbot, SentAttachment(draft.title, draft.subtitle, draft.thumbnail), raw, size
    )


def _assert_original_preview(qtbot, item, raw, size):
    assert item.thumbnail is not None
    assert item.thumbnail.toImage() == QImage.fromData(raw)
    dialog = ImagePreviewDialog(item)
    qtbot.addWidget(dialog)
    dialog.show()
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    image = scroll.widget()
    assert isinstance(image, QLabel)
    assert image._source.size() == QSize(*size)
    if max(size) > 256:
        assert max(image.pixmap().width(), image.pixmap().height()) > 256


@pytest.mark.parametrize("size", [(1600, 900), (900, 1600), (80, 60)])
@pytest.mark.parametrize("load_path", ["conversation", "lazy"])
async def test_stored_preview_keeps_original_resolution(attachment_app, qtbot, size, load_path):
    window, controller, images = attachment_app
    raw = _make_png_bytes(*size)
    attachment = images.import_bytes(raw, source_path="historical.png")
    now = datetime.now(UTC)
    with controller.coordinator()._uow_factory() as uow:
        uow.conversations.add(Conversation("c1", "Image history", now))
        uow.branches.add(Branch("b1", "c1", now))
        uow.messages.add(
            Message("u1", "c1", "b1", MessageRole.USER, "Original image", now, run_id="r1")
        )
        uow.runs.add(RunRecord("r1", "c1", "b1", RunStatus.COMPLETED, now, "u1"))
        uow.snapshots.add_intent(
            RequestIntentSnapshot(
                id="intent1",
                run_id="r1",
                conversation_id="c1",
                branch_id="b1",
                logical_model_id="model",
                endpoint_id="endpoint",
                routing_reason="test",
                created_at=now,
                message_ids=("u1",),
                attachment_ids=(attachment.id,),
            )
        )
        uow.commit()

    # Neither path may reuse the draft-import cache: decode the persisted original.
    if load_path == "conversation":
        window.sidebar.conversation_selected.emit("c1")
        await _until(lambda: controller.conversation_id == "c1" and not window.chat.history_loading)
    else:
        window.chat.add_user_message("Original image", message_id="u1")
    await _until(lambda: bool(window.chat.findChildren(QFrame, "sentAttachment")))
    tiles = window.chat.findChildren(QFrame, "sentAttachment")
    assert len(tiles) == 1
    tile = tiles[0]
    assert tile._item.title == "historical.png"
    assert tile.size() == QSize(56, 56)
    thumbnail = tile.findChild(QLabel).pixmap()
    assert not thumbnail.isNull()
    assert max(thumbnail.width(), thumbnail.height()) <= 52
    _assert_original_preview(qtbot, tile._item, raw, size)
    assert images.read_bytes(attachment.id) == raw
