"""Word attachments: actual binary formats, safe failures and line-read integration."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from limbowave.application.services.file_service import (
    FileReadError,
    FileService,
    RangeLimitExceeded,
)
from limbowave.domain.files import FileKind, UnsupportedFileType, classify_path
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.word_fixtures import doc_bytes, docx_bytes, paragraph


@pytest.fixture
def service() -> FileService:
    return FileService(in_memory_uow_factory(InMemoryStore()))


@pytest.mark.parametrize("suffix", [".doc", ".DOC", ".docx", ".DOCX"])
def test_classify_word(suffix: str) -> None:
    assert classify_path(Path("合同" + suffix)) == FileKind.TEXT


@pytest.mark.parametrize("suffix", [".pdf", ".xls", ".xlsx", ".docm"])
def test_other_office_types_still_rejected(suffix: str) -> None:
    with pytest.raises(UnsupportedFileType):
        classify_path(Path("document" + suffix))


@pytest.mark.parametrize("suffix", [".doc", ".docx"])
def test_word_index_and_range(service: FileService, tmp_path: Path, suffix: str) -> None:
    path = tmp_path / ("中文合同" + suffix)
    raw = (
        doc_bytes([("第一段\r中文与 emoji 😀\r第三段\r", False)])
        if suffix == ".doc"
        else docx_bytes(paragraph("第一段") + paragraph("中文与 emoji 😀") + paragraph("第三段"))
    )
    path.write_bytes(raw)
    card = service.index_path(path)
    assert card.display_name == path.name
    assert card.line_count == 3
    assert card.encoding == "utf-8"
    assert card.size_bytes == len(raw)
    assert card.content_hash == hashlib.sha256(raw).hexdigest()
    result = service.read(card.id, 2, 2)
    assert result.text == "中文与 emoji 😀"
    assert result.actual_range == (2, 2)
    assert not result.changed_since_index


def test_docx_blocks_tables_breaks_and_hyperlinks(service: FileService, tmp_path: Path) -> None:
    body = (
        paragraph("正文")
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("姓名")
        + "</w:tc><w:tc>"
        + paragraph("金额")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + paragraph("小明")
        + "</w:tc><w:tc>"
        + paragraph("100")
        + "</w:tc></w:tr></w:tbl><w:p><w:hyperlink><w:r><w:t>链接文字</w:t>"
        + "<w:tab/><w:t>尾部</w:t><w:br/><w:t>下一行</w:t></w:r></w:hyperlink></w:p>"
        + "<w:p><w:r><w:instrText>PRIVATE FIELD CODE</w:instrText><w:t>字段结果</w:t>"
        + "</w:r><w:del><w:r><w:delText>删除内容</w:delText></w:r></w:del></w:p>"
    )
    path = tmp_path / "table.docx"
    path.write_bytes(docx_bytes(body))
    result = service.read(service.index_path(path).id)
    assert result.text == "正文\n姓名\t金额\n小明\t100\n链接文字\t尾部\n下一行\n字段结果"


@pytest.mark.parametrize("table_name", ["0Table", "1Table"])
def test_doc_mixed_pieces_and_body_only(
    service: FileService, tmp_path: Path, table_name: str
) -> None:
    body = "Latin €\r中文表格\r"
    path = tmp_path / "mixed.doc"
    path.write_bytes(
        doc_bytes(
            [("Latin €\r", True), ("中文表格\rHEADER", False)],
            main_chars=len(body),
            table_name=table_name,
        )
    )
    assert service.read(service.index_path(path).id).text == body.rstrip("\r").replace("\r", "\n")


def test_doc_fields_and_cell_delimiters(service: FileService, tmp_path: Path) -> None:
    path = tmp_path / "fields.doc"
    path.write_bytes(doc_bytes([("名称\x07金额\x07\x07\x13SECRET FIELD\x14可见结果\x15\r", False)]))
    text = service.read(service.index_path(path).id).text
    assert text == "名称\t金额\n可见结果"


@pytest.mark.parametrize("suffix", [".doc", ".docx"])
def test_word_change_detection(service: FileService, tmp_path: Path, suffix: str) -> None:
    def content(text: str) -> bytes:
        return (
            doc_bytes([(text + "\r", False)]) if suffix == ".doc" else docx_bytes(paragraph(text))
        )

    path = tmp_path / ("change" + suffix)
    path.write_bytes(content("旧正文"))
    card = service.index_path(path)
    path.write_bytes(content("新正文"))
    result = service.read(card.id)
    assert result.text == "新正文"
    assert result.changed_since_index
    assert result.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert service.index_path(path).id == card.id
    assert not service.read(card.id).changed_since_index


@pytest.mark.parametrize(
    "suffix,raw",
    [
        (".doc", b"not word"),
        (".docx", b"PK fake"),
        (".doc", doc_bytes([("secret", True)], encrypted=True)),
        (".docx", doc_bytes([("secret", True)], encrypted=True)),
        (".doc", b"{\\rtf1 fake legacy doc}"),
        (".docx", docx_bytes("<broken>")),
    ],
    ids=["bad-doc", "bad-docx", "encrypted-doc", "encrypted-docx", "rtf-doc", "bad-xml"],
)
def test_invalid_word_not_registered(
    service: FileService, tmp_path: Path, suffix: str, raw: bytes
) -> None:
    path = tmp_path / ("invalid" + suffix)
    path.write_bytes(raw)
    with pytest.raises(FileReadError, match="另存为"):
        service.index_path(path)
    assert service.list_all() == []


def test_empty_docx_and_range_limit(service: FileService, tmp_path: Path) -> None:
    path = tmp_path / "empty.docx"
    path.write_bytes(docx_bytes("<w:p/>"))
    card = service.index_path(path)
    assert card.line_count == 0
    assert service.read(card.id).text == ""
    path.write_bytes(docx_bytes("".join(paragraph(str(i)) for i in range(250))))
    card = service.index_path(path)
    assert card.line_count == 250
    with pytest.raises(RangeLimitExceeded):
        service.read(card.id, 1, 201)
    assert service.read(card.id, 200, 202).text == "199\n200\n201"


def test_corrupted_after_index_is_read_error(service: FileService, tmp_path: Path) -> None:
    path = tmp_path / "changed.docx"
    path.write_bytes(docx_bytes(paragraph("ok")))
    card = service.index_path(path)
    path.write_bytes(b"broken")
    with pytest.raises(FileReadError, match="另存为"):
        service.read(card.id)
    assert service.get(card.id) == card


@pytest.mark.parametrize("suffix", [".doc", ".docx"])
def test_word_source_size_limit(service, tmp_path, monkeypatch, suffix):
    from limbowave.application.services import file_service

    monkeypatch.setattr(file_service, "MAX_WORD_BYTES", 10)
    path = tmp_path / ("large" + suffix)
    path.write_bytes(b"x" * 11)
    with pytest.raises(FileReadError, match="拆分"):
        service.index_path(path)
    assert service.list_all() == []


def test_docx_expansion_limit(service, tmp_path, monkeypatch):
    from limbowave.application.services import word_reader

    monkeypatch.setattr(word_reader, "MAX_XML_BYTES", 200)
    path = tmp_path / "large.docx"
    path.write_bytes(docx_bytes(paragraph("a" * 1000)))
    with pytest.raises(FileReadError, match="拆分"):
        service.index_path(path)


@pytest.mark.parametrize(
    "xml",
    [
        b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private">]><a>&x;</a>',
        b"<not-a-word-document/>",
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    ],
    ids=["external-entity", "wrong-root", "missing-body"],
)
def test_docx_unsafe_or_invalid_xml(service, tmp_path, xml):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    path = tmp_path / "unsafe.docx"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(FileReadError, match="另存为"):
        service.index_path(path)


def test_docx_deep_nesting(service, tmp_path):
    path = tmp_path / "nested.docx"
    path.write_bytes(docx_bytes("<w:sdt>" * 70 + paragraph("text") + "</w:sdt>" * 70))
    with pytest.raises(FileReadError, match="嵌套过深"):
        service.index_path(path)


def test_docx_strict_namespace(service, tmp_path):
    import io
    import zipfile

    source = docx_bytes(paragraph("严格格式正文"))
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        xml = original.read("word/document.xml").replace(
            b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            b"http://purl.oclc.org/ooxml/wordprocessingml/main",
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    path = tmp_path / "strict.DOCX"
    path.write_bytes(buffer.getvalue())
    assert service.read(service.index_path(path).id).text == "严格格式正文"


@pytest.mark.parametrize(
    "offset,value", [(0, 0), (2, 0x65), (418, 999999), (422, 0), (76, 99999999)]
)
def test_doc_invalid_headers(service, tmp_path, offset, value):
    import struct

    raw = bytearray(doc_bytes([("text", True)]))
    struct.pack_into("<I", raw, 1024 + offset, value)
    path = tmp_path / "invalid.doc"
    path.write_bytes(raw)
    with pytest.raises(FileReadError, match="另存为"):
        service.index_path(path)


@pytest.mark.parametrize("suffix", [".doc", ".docx"])
def test_word_database_reopen(tmp_path, vault_key, suffix):
    from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

    path = tmp_path / ("persisted" + suffix)
    path.write_bytes(
        doc_bytes([("持久化正文\r", False)])
        if suffix == ".doc"
        else docx_bytes(paragraph("持久化正文"))
    )
    factory = sqlite_uow_factory(tmp_path / "word.db", vault_key)
    card = FileService(factory).index_path(path)
    factory.close()
    reopened = sqlite_uow_factory(tmp_path / "word.db", vault_key)
    try:
        service = FileService(reopened)
        assert service.get(card.id).display_name == path.name
        assert service.read(card.id).text == "持久化正文"
        assert not service.read(card.id).changed_since_index
    finally:
        reopened.close()


def test_doc_unicode_surrogate_pair_across_pieces(service, tmp_path):
    path = tmp_path / "emoji.doc"
    path.write_bytes(doc_bytes([("\ud83d", False), ("\ude00\r", False)]))
    assert service.read(service.index_path(path).id).text == "😀"


def test_docx_corrupt_deflate_becomes_read_error(service, tmp_path):
    import io
    import zipfile

    raw = bytearray(docx_bytes(paragraph("text")))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("word/document.xml")
        start = info.header_offset + 30 + len(info.filename.encode())
    raw[start] = (raw[start] & ~7) | 7  # DEFLATE reserved block type
    path = tmp_path / "corrupt.docx"
    path.write_bytes(raw)
    with pytest.raises(FileReadError, match="另存为"):
        service.index_path(path)
