"""Attachment services accept storage capabilities, not concrete encrypted stores."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from limbowave.application.services.attachment_gc import AttachmentGarbageCollector
from limbowave.application.services.clipboard_service import ClipboardService
from limbowave.application.services.file_service import FileReadError, FileService
from limbowave.application.services.image_service import ImageService
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZxkAAAAASUVORK5CYII="
)


class MemoryBlobs:
    """Test-only storage: no filesystem, vault, inheritance, or runtime type checks."""

    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def get(self, blob_id: str) -> bytes:
        return self.contents[blob_id]

    def put(self, content: bytes) -> str:
        blob_id = hashlib.sha256(content).hexdigest()
        self.contents[blob_id] = content
        return blob_id

    def delete(self, blob_id: str) -> bool:
        return self.contents.pop(blob_id, None) is not None


class ReadOnlyBlobs:
    """Readers do not need write, delete, or even existence-check capabilities."""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents

    def get(self, blob_id: str) -> bytes:
        return self.contents[blob_id]


def test_clipboard_round_trip_uses_only_reader_capability() -> None:
    factory = in_memory_uow_factory()
    blobs = MemoryBlobs()
    document = ClipboardService(factory, blobs).save("第一行\r\n第二行\r第三行\n")
    reader = FileService(factory, blob_store=ReadOnlyBlobs(blobs.contents))

    result = reader.read(document.id, 2, 3)

    assert result.text == "第二行\n第三行"
    assert result.actual_range == (2, 3)
    assert result.total_lines == document.line_count == 3
    assert not result.changed_since_index


def test_reader_preserves_missing_blob_error() -> None:
    factory = in_memory_uow_factory()
    document = ClipboardService(factory, MemoryBlobs()).save("text")
    reader = FileService(factory, blob_store=ReadOnlyBlobs({}))

    with pytest.raises(KeyError):
        reader.read(document.id)


def test_reader_preserves_storage_failure() -> None:
    failure = OSError("storage unavailable")

    class BrokenReader:
        def get(self, blob_id: str) -> bytes:
            raise failure

    factory = in_memory_uow_factory()
    document = ClipboardService(factory, MemoryBlobs()).save("text")
    reader = FileService(factory, blob_store=BrokenReader())
    with pytest.raises(OSError) as caught:
        reader.read(document.id)
    assert caught.value is failure


def test_unconfigured_blob_reader_still_rejects_clipboard_documents() -> None:
    factory = in_memory_uow_factory()
    document = ClipboardService(factory, MemoryBlobs()).save("text")
    with pytest.raises(FileReadError, match="blob"):
        FileService(factory).read(document.id)


def test_disk_documents_do_not_require_a_blob_reader(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo", encoding="utf-8")
    service = FileService(in_memory_uow_factory())
    document = service.index_path(path)
    assert service.read(document.id, 2, 2).text == "two"


def test_image_service_accepts_structural_storage() -> None:
    blobs = MemoryBlobs()
    service = ImageService(in_memory_uow_factory(), blobs)
    attachment = service.import_bytes(PNG)
    assert service.read_bytes(attachment.id) == PNG
    assert service.delete(attachment.id)
    assert not blobs.contents


def test_clipboard_delete_accepts_structural_storage() -> None:
    blobs = MemoryBlobs()
    service = ClipboardService(in_memory_uow_factory(), blobs)
    document = service.save("text")
    assert service.delete(document.id)
    assert not blobs.contents


def test_collector_needs_only_prune_and_preserves_protected_documents() -> None:
    class Pruner:
        def __init__(self) -> None:
            self.retained: set[str] | None = None

        def prune(self, retained: set[str]) -> int:
            self.retained = retained
            return 2

    factory = in_memory_uow_factory()
    service = ClipboardService(factory, MemoryBlobs())
    protected = service.save("keep")
    orphan = service.save("remove")
    pruner = Pruner()
    result = AttachmentGarbageCollector(factory, pruner).collect(
        protected=frozenset({protected.id})
    )
    assert result.registrations == 1
    assert result.blobs == 2
    assert pruner.retained == {protected.blob_id}
    with factory() as uow:
        assert uow.file_documents.get(protected.id) is not None
        assert uow.file_documents.get(orphan.id) is None
