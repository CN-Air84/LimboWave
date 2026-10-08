"""Drafts must stay with their conversation/branch, never with the active runtime."""

import asyncio

import pytest
from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QImage

from limbowave.ui.chat_view import ChatView
from tests.ui.test_session_switch_during_generation import browse_history, wait_for, wired

__all__ = ["wired"]


def test_draft_only_preview_allows_text_but_never_submits(qtbot):
    view = ChatView(draft_only=True)
    qtbot.addWidget(view)
    view.show()
    sent = []
    files, images = [], []
    view.message_submitted.connect(sent.append)
    view.files_dropped.connect(files.append)
    view.image_pasted.connect(images.append)
    view.set_available(True)
    view.set_history_loading(True)
    assert not view._input.isEnabled()
    view.set_history_loading(False)
    assert view._input.isEnabled()
    assert view._composer.isEnabled()
    view._input.setPlainText("草稿\nsecond line")
    view._on_send()
    qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert view._input.toPlainText() == "草稿\nsecond line"
    assert not sent
    assert not view._send_btn.isEnabled()
    for control in (view._attach_btn, view._permission_btn, view._logical_model,
                    view._advanced_btn, view._compress_btn):
        assert not control.isEnabled()
    assert view._draft_notice.isVisible()
    assert "不会自动发送" in view._draft_notice.text()
    text = QMimeData()
    text.setText("pasted text")
    view._input.insertFromMimeData(text)
    assert "pasted text" in view._input.toPlainText()
    document = QMimeData()
    document.setUrls([QUrl.fromLocalFile("C:/draft.txt")])
    image = QMimeData()
    image.setImageData(QImage(2, 2, QImage.Format.Format_RGB32))
    for mime in (document, image):
        assert not view._input.canInsertFromMimeData(mime)
        view._input.insertFromMimeData(mime)
    assert not files and not images


async def test_preview_drafts_survive_switches_and_completion(wired):
    window, controller, kernel, first_id, _, second_id = wired
    live_id = controller.conversation_id
    window.chat._input.setPlainText("live draft")
    window.chat.attachments.add_attachment("live-file", "local file", "only for live")
    await browse_history(window, first_id)
    window.history_preview._input.setPlainText("first draft")
    await browse_history(window, second_id)
    assert window.history_preview._input.toPlainText() == ""
    window.history_preview._input.setPlainText("second draft")
    await browse_history(window, first_id)
    assert window.history_preview._input.toPlainText() == "first draft"
    await browse_history(window, live_id)
    assert window.chat._input.toPlainText() == "live draft"
    assert window.chat.attachments.attachment_ids() == ["live-file"]
    await browse_history(window, first_id)
    assert window.history_preview._input.toPlainText() == "first draft"
    kernel.say("background done")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: controller.conversation_id == first_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert window.chat._input.toPlainText() == "first draft"
    assert window.chat._send_btn.isEnabled()
    assert not window.chat.attachments.attachment_ids()
    assert len(window.chat._rows) == 2  # Nothing was automatically sent.
    await browse_history(window, live_id)
    assert window.chat._input.toPlainText() == "live draft"
    assert window.chat.attachments.attachment_ids() == ["live-file"]
    await browse_history(window, second_id)
    assert window.chat._input.toPlainText() == "second draft"
    window.chat._input.clear()
    await browse_history(window, first_id)
    await browse_history(window, second_id)
    assert window.chat._input.toPlainText() == ""


async def test_failed_navigation_and_restore_keep_draft(wired, monkeypatch):
    from limbowave.domain.runtime_state import RuntimeRestoreResult

    window, controller, kernel, history_id, _, _ = wired
    live_id = controller.conversation_id
    await browse_history(window, history_id)
    preview = window.history_preview
    preview._input.setPlainText("never lose this")
    window.sidebar.conversation_selected.emit("missing")
    await wait_for(lambda: len(preview._rows) == 3 and not window.chat.history_loading)
    assert preview._input.toPlainText() == "never lose this"
    original = kernel.restore_runtime_state

    async def fail(_snapshot):
        return RuntimeRestoreResult(success=False, error="refused")

    monkeypatch.setattr(kernel, "restore_runtime_state", fail)
    kernel.say("done")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: len(preview._rows) == 4 and not window.chat.history_loading)
    assert controller.conversation_id == live_id
    assert preview._input.toPlainText() == "never lose this"
    assert preview._input.isEnabled()
    assert not preview._send_btn.isEnabled()
    assert "重新打开" in preview._draft_notice.text()
    monkeypatch.setattr(kernel, "restore_runtime_state", original)
    window.sidebar.conversation_selected.emit(history_id)
    await wait_for(lambda: window.history_preview is None and not window.chat.history_loading)
    assert window.chat._input.toPlainText() == "never lose this"


async def test_new_session_does_not_inherit_previous_draft(wired):
    window, controller, kernel, history_id, _, _ = wired
    await browse_history(window, history_id)
    window.history_preview._input.setPlainText("history draft")
    kernel.say("done")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await wait_for(lambda: window.history_preview is None and not window.chat.history_loading)
    window.sidebar.new_conversation_requested.emit()
    await wait_for(lambda: controller.conversation_id is None)
    assert window.chat._input.toPlainText() == ""
    await browse_history(window, history_id)
    assert window.chat._input.toPlainText() == "history draft"


async def test_drafts_are_isolated_between_branches_of_one_conversation(wired):
    window, controller, kernel, history_id, old_branch, _ = wired
    kernel.say("done")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    await browse_history(window, history_id)
    message_id = window.chat._rows[0]._message_id
    await controller.edit_user_message(message_id, "edited question")
    kernel.say("edited answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    new_branch = controller.branch_id
    assert new_branch != old_branch
    window.sidebar.new_conversation_requested.emit()
    await wait_for(lambda: controller.conversation_id is None)
    await controller.send("another live run")

    async def browse_branch(branch_id):
        window.sidebar.branch_switch_requested.emit(history_id, branch_id)
        await wait_for(lambda: window.sidebar._active_branch_id == branch_id
                       and not window.chat.history_loading)

    await browse_branch(old_branch)
    window.history_preview._input.setPlainText("old branch draft")
    await browse_branch(new_branch)
    assert window.history_preview._input.toPlainText() == ""
    window.history_preview._input.setPlainText("new branch draft")
    await browse_branch(old_branch)
    assert window.history_preview._input.toPlainText() == "old branch draft"
    await browse_branch(new_branch)
    assert window.history_preview._input.toPlainText() == "new branch draft"


@pytest.mark.parametrize("during_render", [False, True])
async def test_completion_during_navigation_preserves_both_drafts(
    wired, monkeypatch, during_render
):
    window, controller, kernel, first_id, _, second_id = wired
    await browse_history(window, first_id)
    window.history_preview._input.setPlainText("first draft")
    await browse_history(window, second_id)
    window.history_preview._input.setPlainText("second draft")
    await browse_history(window, first_id)
    entered, release = asyncio.Event(), asyncio.Event()
    if during_render:
        original = ChatView.load_history_incrementally

        async def slow_render(view, *args, **kwargs):
            if view._draft_only:
                entered.set()
                await release.wait()
            return await original(view, *args, **kwargs)

        monkeypatch.setattr(ChatView, "load_history_incrementally", slow_render)
    else:
        from limbowave import app

        original = app.HistoryReader.read

        async def slow_read(reader, operation, *args, **kwargs):
            result = await original(reader, operation, *args, **kwargs)
            if operation.__name__ == "_read_history_view":
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(app.HistoryReader, "read", slow_read)
    window.sidebar.conversation_selected.emit(second_id)
    await asyncio.wait_for(entered.wait(), 2)
    kernel.say("completed during navigation")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    release.set()
    await wait_for(lambda: controller.conversation_id == second_id
                   and window.history_preview is None and not window.chat.history_loading)
    assert window.chat._input.toPlainText() == "second draft"
    await browse_history(window, first_id)
    assert window.chat._input.toPlainText() == "first draft"


def test_composer_snapshot_preserves_edit_and_attachment_state(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.begin_edit("old-message", "edited draft")
    view.attachments.add_attachment("file", "title", "detail")
    draft = view.snapshot_draft()
    view.cancel_edit()
    view.set_draft(draft)
    assert view._input.toPlainText() == "edited draft"
    assert view.editing_message_id == "old-message"
    assert view.attachments.attachment_ids() == ["file"]
    assert view.attachments.snapshot() == draft.attachments


@pytest.mark.parametrize("state", ["read_only", "busy", "unavailable"])
def test_blocked_send_handler_never_clears_a_draft(qtbot, state):
    view = ChatView(read_only=state == "read_only")
    qtbot.addWidget(view)
    if state == "busy":
        view.set_busy(True)
    if state == "unavailable":
        view.set_available(False)
    submitted = []
    view.message_submitted.connect(submitted.append)
    view._input.setPlainText("keep this draft")
    view._on_send()
    assert not submitted
    assert view._input.toPlainText() == "keep this draft"
