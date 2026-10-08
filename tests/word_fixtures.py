"""Small self-contained OOXML and OLE Word fixtures (no Office installation)."""

from __future__ import annotations

import io
import struct
import zipfile
from xml.sax.saxutils import escape


def docx_bytes(body: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
            ),
        )
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>"
            ),
        )
    return stream.getvalue()


def paragraph(text: str) -> str:
    return "<w:p><w:r><w:t>" + escape(text) + "</w:t></w:r></w:p>"


def doc_bytes(
    pieces: list[tuple[str, bool]],
    *,
    encrypted: bool = False,
    main_chars: int | None = None,
    table_name: str = "1Table",
) -> bytes:
    """Word 97 FIB + mixed compressed/UTF-16 pieces in real CFB streams."""
    word = bytearray(4096)
    struct.pack_into("<HH", word, 0, 0xA5EC, 0xC1)
    flags = 4 | (0x200 if table_name == "1Table" else 0) | (0x100 if encrypted else 0)
    struct.pack_into("<H", word, 10, flags)
    struct.pack_into("<H", word, 32, 14)  # csw
    struct.pack_into("<H", word, 62, 22)  # cslw
    struct.pack_into("<H", word, 152, 93)  # cbRgFcLcb
    cps = [0]
    pcds = bytearray()
    offset = 1024
    for text, compressed in pieces:
        raw = (
            text.encode("cp1252")
            if compressed
            else text.encode("utf-16-le", errors="surrogatepass")
        )
        word[offset : offset + len(raw)] = raw
        fc = (offset * 2) | 0x40000000 if compressed else offset
        pcds.extend(struct.pack("<HIH", 0, fc, 0))
        cps.append(cps[-1] + (len(raw) if compressed else len(raw) // 2))
        offset += len(raw)
    assert len(word) == 4096 and offset <= 4096
    struct.pack_into("<I", word, 76, cps[-1] if main_chars is None else main_chars)
    plc = struct.pack("<" + "I" * len(cps), *cps) + pcds
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 418, 0, len(clx))  # fcClx/lcbClx, entry 33
    table = clx.ljust(4096, b"\x00")
    end, free = 0xFFFFFFFE, 0xFFFFFFFF

    def directory(
        name: str, kind: int, start: int, size: int, child: int = free, right: int = free
    ) -> bytes:
        entry = bytearray(128)
        encoded = (name + "\x00").encode("utf-16-le")
        entry[: len(encoded)] = encoded
        struct.pack_into("<HBBIII", entry, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", entry, 116, start, size)
        return bytes(entry)

    directories = (
        directory("Root Entry", 5, end, 0, child=1)
        + directory("WordDocument", 2, 1, 4096, right=2)
        + directory(table_name, 2, 9, 4096)
    ).ljust(512, b"\x00")
    fat = [end, *range(2, 9), end, *range(10, 17), end, 0xFFFFFFFD]
    fat.extend([free] * (128 - len(fat)))
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 17, *([free] * 108))
    return bytes(header) + directories + bytes(word) + table + struct.pack("<128I", *fat)
