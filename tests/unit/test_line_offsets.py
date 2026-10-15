"""Shared byte-oriented line indexing preserves the existing numbering contract."""

import pytest

from limbowave.domain.files import compute_line_offsets


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", ()),
        (b"a", (0,)),
        (b"a\n", (0,)),
        (b"\n", (0,)),
        (b"\r", (0,)),
        (b"\r\n", (0,)),
        (b"a\nb", (0, 2)),
        (b"a\rb", (0, 2)),
        (b"a\r\nb", (0, 3)),
        (b"a\r\nb\nc\rd", (0, 3, 5, 7)),
        (b"\n\n", (0, 1)),
        (b"\r\n\r\n", (0, 2)),
        ("甲\r\n乙\n丙".encode(), (0, 5, 9)),
        (b"a\x0bb\x0cc", (0,)),  # Only CR/LF, not bytes.splitlines semantics.
    ],
)
def test_compute_line_offsets(raw: bytes, expected: tuple[int, ...]) -> None:
    assert compute_line_offsets(raw) == expected
