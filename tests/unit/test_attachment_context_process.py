"""The real storage worker reconstructs attachments without main-loop repository access."""

from limbowave.application.services.file_service import FileService
from tests.unit.test_conversation_process import process_stack as process_stack


async def test_real_worker_rebuilds_followup_manifest(process_stack, tmp_path, monkeypatch):
    coord, kernel, factory, _, _ = process_stack
    path = tmp_path / "persisted.md"
    path.write_text("private document bytes", encoding="utf-8")
    document = FileService(factory).index_path(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("attachment projection ran on the main process")

    monkeypatch.setattr(coord, "_attachment_prompt", forbidden)
    for text, attachments in (("read", [document.id]), ("continue", [])):
        await coord.send(text, attachment_ids=attachments)
        kernel.say("reply")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        assert kernel.attachment_context["documents"] == [{
            "file_id": document.id, "name": document.display_name,
            "path": document.path, "line_count": document.line_count,
        }]
    assert kernel.sent == ["read", "continue"]
