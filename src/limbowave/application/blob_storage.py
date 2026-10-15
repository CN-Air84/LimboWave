"""Attachment storage capabilities owned by the application layer.

Composition roots supply the encrypted implementation. Services neither manage
keys/paths nor require inheritance from a concrete storage class. Read-only
operations must not require write or deletion capabilities.
"""

from typing import Protocol


class BlobReader(Protocol):
    """Read original bytes by opaque ID; missing blobs raise KeyError.

    Integrity and I/O failures propagate from the implementation. A service may
    deliberately tolerate them (for example, skipping unavailable export images).
    """

    def get(self, blob_id: str) -> bytes: ...


class BlobStore(BlobReader, Protocol):
    """Store and delete attachment bytes; production implementations encrypt them."""

    def put(self, content: bytes) -> str:
        """Return an opaque, stable content ID; identical content is idempotent."""
        ...

    def delete(self, blob_id: str) -> bool:
        """Return whether a blob was removed; missing blobs return False."""
        ...


class BlobPruner(Protocol):
    """Garbage collection capability, separate from normal attachment access."""

    def prune(self, retained: set[str]) -> int:
        """Remove unreferenced blobs and return their count.

        The caller must own a quiescent library: all producers/importers are
        stopped, and the retained IDs were read from committed metadata.
        """
        ...
