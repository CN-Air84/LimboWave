"""Reclaim orphan attachments only while every producer/importer is stopped.

The caller must own the library and drain background work first. Metadata is
committed before unlinking blobs, so interruption leaves reclaimable files, not
live rows pointing at removed files. Content-addressed blobs may be shared by
images and documents; both tables participate in the final reachability check.
"""

from dataclasses import dataclass

from limbowave.application.blob_storage import BlobPruner
from limbowave.application.repositories import UnitOfWorkFactory


@dataclass(frozen=True)
class CollectionResult:
    registrations: int
    blobs: int


class AttachmentGarbageCollector:
    def __init__(self, factory: UnitOfWorkFactory, blobs: BlobPruner) -> None:
        self.factory = factory
        self.blobs = blobs

    def collect(self, *, protected: frozenset[str] = frozenset()) -> CollectionResult:
        with self.factory() as uow:
            referenced = set(protected)
            for intent in uow.snapshots.list_all_intents():
                referenced.update(intent.attachment_ids)
            removed = 0
            for repository in (uow.image_attachments, uow.file_documents):
                for attachment in repository.list_all():
                    if attachment.id not in referenced:
                        repository.delete(attachment.id)
                        removed += 1
            uow.commit()
        # Re-open after commit (also supports staged in-memory repositories).
        with self.factory() as uow:
            retained = {a.blob_id for a in uow.image_attachments.list_all()}
            retained.update(d.blob_id for d in uow.file_documents.list_all() if d.blob_id)
        return CollectionResult(removed, self.blobs.prune(retained))
