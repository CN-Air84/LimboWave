"""Mini mode uses real application wiring with a local deterministic kernel."""
from __future__ import annotations

from tests.ui.test_history_loading import _until
from tests.ui.test_send_queue_integration import (
    browse_history,
    settle,
    wait_for,
)
from tests.ui.test_send_queue_integration import (
    wired_queue as queue_fixture,
)

wired_queue = queue_fixture


async def test_mini_queues_once_to_history_and_preserves_both_drafts(wired_queue):
    window, controller, kernel, first, _, _ = wired_queue
    live_id = controller.conversation_id
    window.chat._input.setPlainText("live draft")
    window._companion.open()
    mini = window._companion.window
    await browse_history(window, first[0])
    window._companion.refresh()
    assert mini._input.document() is window.history_preview._input.document()
    mini._input.setPlainText("queued from mini")
    assert mini._send.isEnabled()
    mini._send.click()
    mini._send.click()  # Empty composer cannot create a second request.
    assert mini._input.toPlainText() == ""
    mini._input.setPlainText("new history draft")
    await settle(controller, kernel)
    await wait_for(lambda: "queued from mini" in kernel.sent)
    assert kernel.sent.count("queued from mini") == 1
    assert controller.conversation_id == first[0]
    await settle(controller, kernel)
    await wait_for(lambda: window.history_preview is None)
    assert mini._input.toPlainText() == "new history draft"
    window._companion.restore()
    await browse_history(window, live_id)
    assert window.chat._input.toPlainText() == "live draft"
    assert kernel.sent.count("queued from mini") == 1


async def test_mini_streams_while_main_is_hidden_and_stop_uses_live_run(wired_queue):
    window, controller, kernel, _, _, _ = wired_queue
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit("message.update", {
        "assistantMessageEvent": {"type": "text_delta", "delta": "before"}
    })
    window._companion.open()
    mini = window._companion.window
    kernel.emit("message.update", {
        "assistantMessageEvent": {"type": "text_delta", "delta": " after"}
    })
    window._companion.refresh()
    assert any("before after" in text for _, text in mini.messages)
    assert not window.isVisible()
    mini._stop.click()
    await wait_for(lambda: kernel.aborted)
    kernel.say("before after", stop="aborted")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    assert not controller.busy
    assert mini.isVisible()


async def test_mini_picker_imports_and_sends_real_image_and_document(
    wired_queue, tmp_path, monkeypatch,
):
    import base64
    from dataclasses import replace

    from PySide6.QtWidgets import QFileDialog

    from tests.ui.test_attachment_bar import _make_png_bytes

    window, controller, kernel, _, _, _ = wired_queue
    await settle(controller, kernel)
    # The queue fixture is text-only; this local kernel accepts image payloads.
    coordinator = controller.coordinator()
    original_context = coordinator._context
    monkeypatch.setattr(coordinator, "_context",
                        lambda: replace(original_context(), supports_images=True))
    image = _make_png_bytes(160, 90)
    image_path = tmp_path / "mini-photo.png"
    image_path.write_bytes(image)
    document_path = tmp_path / "mini-notes.txt"
    document_path.write_text("A real document attached from the mini window.", encoding="utf-8")
    window._companion.open()
    mini = window._companion.window
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *_args: (
        [str(image_path), str(document_path)], ""))
    mini._attach.click()
    await _until(lambda: len(window.chat.attachments.attachment_ids()) == 2)
    window._companion.refresh()
    assert mini._attachments.attachment_ids() == window.chat.attachments.attachment_ids()
    assert {item.title for item in mini._attachments.snapshot()} == {
        "mini-photo.png", "mini-notes.txt"}
    mini._input.setPlainText("sent from mini with attachments")
    mini._send.click()
    await _until(lambda: any("sent from mini with attachments" in text for text in kernel.sent))
    assert sum("sent from mini with attachments" in text for text in kernel.sent) == 1
    assert base64.b64decode(kernel.sent_images[-1][0]["data"]) == image
    await _until(lambda: not window.chat.attachments.attachment_ids())
    window._companion.refresh()
    assert not mini._attachments.attachment_ids()
    await settle(controller, kernel)


async def test_mini_pasted_image_reaches_real_attachment_store(wired_queue):
    from PySide6.QtCore import QMimeData, QSize
    from PySide6.QtGui import QImage

    from tests.ui.test_attachment_bar import _make_png_bytes

    window, controller, kernel, _, _, _ = wired_queue
    await settle(controller, kernel)
    window._companion.open()
    mini = window._companion.window
    mime = QMimeData()
    mime.setImageData(QImage.fromData(_make_png_bytes(120, 75)))
    mini._input.insertFromMimeData(mime)
    await _until(lambda: len(window.chat.attachments.attachment_ids()) == 1)
    window._companion.refresh()
    item = mini._attachments.snapshot()[0]
    assert item.thumbnail.size() == QSize(120, 75)
    payload = controller.coordinator().attachment_builder([item.attachment_id])
    assert len(payload.images) == 1
