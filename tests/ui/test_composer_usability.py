"""User-facing feedback and non-destructive composer interactions."""

import pytest
from PySide6.QtCore import QMimeData, Qt, QUrl

from limbowave.ui.chat_view import ChatView, ComposerDraft


def test_status_updates_preserve_draft_without_reintroducing_feedback_row(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view._input.setPlainText("不要改动草稿")
    for status in ("备份完成：<draft>.zip", "不能删除唯一分支", "就绪"):
        view.set_status(status)
        assert view.status_text == status
        assert view._input.toPlainText() == "不要改动草稿"
    assert not hasattr(view, "_feedback")


def test_unavailable_allows_drafting_but_not_send_or_file_paste(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    sent, files = [], []
    view.message_submitted.connect(sent.append)
    view.files_dropped.connect(files.append)
    view.set_available(False, "请先在设置中添加模型")
    assert view._input.isEnabled()
    assert "请先在设置中添加模型" in view.status_text
    assert "草稿" in view.status_text
    view._input.setPlainText("待发送")
    qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert not sent
    assert view._input.toPlainText() == "待发送"
    assert not view._send_btn.isEnabled()
    assert not view._attach_btn.isEnabled()
    assert not view._composer.acceptDrops()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("C:/unavailable.txt")])
    view._input.insertFromMimeData(mime)
    assert not files
    view.set_available(True)
    assert view._send_btn.isEnabled()
    assert view._input.attachments_enabled
    assert view._composer.acceptDrops()
    assert not sent
    assert view._input.toPlainText() == "待发送"


@pytest.mark.parametrize("state", ["read_only", "loading", "compressing"])
def test_drafting_does_not_bypass_read_only_or_transition_locks(qtbot, state):
    view = ChatView(read_only=state == "read_only")
    qtbot.addWidget(view)
    view.set_available(False)
    if state == "loading":
        view.set_history_loading(True)
    elif state == "compressing":
        view.begin_compression(40)
    assert not view._input.isEnabled()


def test_cancel_edit_restores_original_text_and_attachments(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("未发送的新问题")
    view.attachments.add_attachment("draft-file", "草稿.txt", "2 行")
    original = view.snapshot_draft()
    view.begin_edit("message-1", "旧问题")
    view.attachments.clear()
    view.attachments.add_attachment("old-file", "旧文件.txt", "1 行")
    view._input.setPlainText("修改旧问题")
    view.cancel_edit()
    assert view.snapshot_draft() == original


def test_edit_backup_survives_multiple_edits_and_conversation_switches(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("原草稿")
    view.begin_edit("message-1", "第一个问题")
    view.begin_edit("message-2", "第二个问题")
    editing = view.snapshot_draft()
    view.set_draft(ComposerDraft("其他会话"))
    view.cancel_edit()
    assert view._input.toPlainText() == "其他会话"
    view.set_draft(editing)
    view.cancel_edit()
    assert view._input.toPlainText() == "原草稿"
    assert view.editing_message_id is None


def test_plain_empty_draft_does_not_resurrect_previous_edit_backup(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("之前的草稿")
    view.begin_edit("m", "编辑内容")
    view.set_draft(ComposerDraft())
    view.begin_edit("other", "另一个问题")
    view.cancel_edit()
    assert view.snapshot_draft() == ComposerDraft()


async def test_app_saves_draft_before_loading_historical_attachments(qtbot):
    from types import SimpleNamespace
    from typing import Any

    from tests.ui.test_business_offload import app_callback

    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("原草稿")
    view.attachments.add_attachment("draft-file", "draft.txt", "draft")
    original = view.snapshot_draft()

    async def read(_operation):
        return SimpleNamespace(content="旧问题"), {"history-file": {}}

    prepare = app_callback("_prepare_edit", {
        "Any": Any,
        "detached_scope": None,
        "_display_scope": lambda: ("conversation", "branch"),
        "chat": view,
        "history_reader": SimpleNamespace(read=read),
        "_add_imported_attachment": lambda aid, _item: view.attachments.add_attachment(
            aid, "history.txt", "history"
        ),
    })
    await prepare("old-message")
    assert view._input.toPlainText() == "旧问题"
    assert view.attachments.attachment_ids() == ["history-file"]
    view.cancel_edit()
    assert view.snapshot_draft() == original
