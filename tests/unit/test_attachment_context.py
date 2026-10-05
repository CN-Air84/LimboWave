"""Attachment references come from durable branch records, not model summaries."""

from dataclasses import replace

import pytest

from limbowave.application.services.file_service import FileService
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.files import FileKind
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel
from tests.unit.test_run_coordinator import _resume_context


class AttachmentKernel(TreeKernel):
    def __init__(self):
        super().__init__()
        self.attachment_contexts = []

    async def set_attachment_context(self, context):
        self.attachment_contexts.append(context)


@pytest.fixture
def stack(tmp_path):
    factory = in_memory_uow_factory(InMemoryStore())
    files = FileService(factory)
    docs = []
    for name in ("original", "sibling", "unattached"):
        path = tmp_path / f"{name}.md"
        path.write_text(f"PRIVATE_CONTENT_{name}", encoding="utf-8")
        docs.append(files.index_path(path))
    kernel = AttachmentKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    return factory, files, docs, kernel, coord


async def finish(kernel, coord):
    kernel.say("reply")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


def ids(context):
    return [d["file_id"] for d in context["documents"]]


async def test_followup_keeps_original_attachment_id_and_only_metadata(stack):
    factory, _, docs, kernel, coord = stack
    first, _, _ = docs
    await coord.send("read", attachment_ids=[first.id, first.id])
    await finish(kernel, coord)
    await coord.send("continue from line 58001")
    await finish(kernel, coord)
    assert len(kernel.attachment_contexts) == 2
    context = kernel.attachment_contexts[-1]
    assert ids(context) == [first.id]
    assert context["branch_id"] == coord.branch_id
    assert context["documents"][0]["name"] == first.display_name
    assert context["documents"][0]["line_count"] == first.line_count
    assert "PRIVATE_CONTENT" not in str(context)
    messages = HistoryService(factory).branch_messages(coord.branch_id)
    assert [m.content for m in messages if m.role.value == "user"] == [
        "read", "continue from line 58001"
    ]


async def test_branch_prefix_inherits_attachments_but_not_sibling_tail(stack):
    factory, _, docs, kernel, coord = stack
    first, sibling, _ = docs
    await coord.send("first", attachment_ids=[first.id])
    await finish(kernel, coord)
    await coord.send("second", attachment_ids=[sibling.id])
    await finish(kernel, coord)
    original_branch = coord.branch_id
    messages = HistoryService(factory).branch_messages(original_branch)
    assert await coord.edit_user_message(messages[2].id, "second edited")
    await finish(kernel, coord)
    assert ids(kernel.attachment_contexts[-1]) == [first.id]
    assert await coord.switch_branch(original_branch)
    await coord.send("continue original")
    await finish(kernel, coord)
    assert ids(kernel.attachment_contexts[-1]) == [first.id, sibling.id]


async def test_deleted_and_non_document_ids_are_not_advertised(stack):
    factory, _, docs, kernel, coord = stack
    first = docs[0]
    await coord.send("read", attachment_ids=[first.id, "image_unknown"])
    await finish(kernel, coord)
    with factory() as uow:
        uow.file_documents.delete(first.id)
        uow.commit()
    await coord.send("continue")
    await finish(kernel, coord)
    assert ids(kernel.attachment_contexts[-1]) == []


async def test_clipboard_document_does_not_require_disk_path(stack):
    factory, _, docs, kernel, coord = stack
    clipboard = replace(docs[0], id="clipboard", kind=FileKind.CLIPBOARD, path=None)
    with factory() as uow:
        uow.file_documents.add(clipboard)
        uow.commit()
    await coord.send("read clipboard", attachment_ids=[clipboard.id])
    await finish(kernel, coord)
    assert ids(kernel.attachment_contexts[-1]) == [clipboard.id]
    assert kernel.attachment_contexts[-1]["documents"][0]["path"] is None


async def test_sync_failure_does_not_send_without_attachment_context(stack):
    _, _, docs, kernel, coord = stack
    async def fail(context):
        raise RuntimeError("attachment context failed")
    kernel.set_attachment_context = fail
    await coord.send("read", attachment_ids=[docs[0].id])
    await coord.wait_idle()
    assert kernel.sent == []


async def test_reopened_conversation_rebuilds_references_without_reupload(stack):
    factory, _, docs, kernel, coord = stack
    await coord.send("read", attachment_ids=[docs[0].id])
    await finish(kernel, coord)
    fresh_kernel = AttachmentKernel()
    reopened = RunCoordinator(fresh_kernel, factory, context=_resume_context)
    assert await reopened.resume(coord.conversation_id, coord.branch_id)
    await reopened.send("continue after restart")
    await finish(fresh_kernel, reopened)
    assert ids(fresh_kernel.attachment_contexts[-1]) == [docs[0].id]
    assert await reopened.new_session()
    await reopened.send("unrelated new conversation")
    await finish(fresh_kernel, reopened)
    assert ids(fresh_kernel.attachment_contexts[-1]) == []
