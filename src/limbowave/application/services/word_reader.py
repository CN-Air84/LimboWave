"""Bounded, offline Word body-text extraction; never opens Office or executes macros.

DOCX: paragraphs/tables in body order, without loading external relationships.
DOC: Word 97–2003 FIB/CLX piece tables (MS-DOC), main story only.
Both produce UTF-8-readable text, not a rendering of Word pages.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from itertools import pairwise
from xml.etree.ElementTree import Element, ParseError

import olefile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_WORD_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 16 * 1024 * 1024
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_REMEDY = "请用 Word/WPS 打开，解除密码保护后另存为 .docx 或 .txt 再添加。"


class WordExtractionError(Exception):
    """Invalid, encrypted, oversized, or unsupported Word document."""


def extract_word_text(raw: bytes, suffix: str) -> str:
    """Validate the format and return only extracted body text, or an actionable error."""
    try:
        if len(raw) > MAX_WORD_BYTES:
            raise ValueError("文件超过 32 MiB，请拆分后重试")
        if suffix.lower() == ".docx":
            if raw.startswith(OLE_SIGNATURE):
                raise ValueError("DOCX 已加密或实际格式与扩展名不符")
            text = _read_docx(raw)
        elif suffix.lower() == ".doc":
            text = _read_doc(raw)
        else:
            raise ValueError("不是受支持的 Word 文件")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("提取的文字过多，请拆分文档")
        return text.rstrip("\n")
    except (
        ValueError,
        OSError,
        KeyError,
        struct.error,
        UnicodeError,
        ParseError,
        DefusedXmlException,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        NotImplementedError,
        IndexError,
        OverflowError,
    ) as exc:
        # Do not expose parser internals or document text in the UI error.
        reason = str(exc) if type(exc) is ValueError else "文件损坏或格式无法解析"
        raise WordExtractionError(f"无法读取 Word 文档：{reason}。{_REMEDY}") from exc


def _name(element: Element) -> str:
    namespace, _, name = element.tag.removeprefix("{").partition("}")
    return name if namespace in _NAMESPACES else ""


def _read_docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 10000:
            raise ValueError("文档内部文件过多，请拆分文档")
        # Never extract to disk or resolve relationship targets (including external URLs).
        parts = [info for info in infos if info.filename == "word/document.xml"]
        if len(parts) != 1:
            raise ValueError("DOCX 缺少唯一的正文")
        info = parts[0]
        if info.flag_bits & 1:
            raise ValueError("文档已加密")
        if info.file_size > MAX_XML_BYTES:
            raise ValueError("解压后的正文超过 32 MiB，请拆分文档")
        with archive.open(info) as stream:
            xml = stream.read(MAX_XML_BYTES + 1)
        if len(xml) > MAX_XML_BYTES:
            raise ValueError("解压后的正文过大，请拆分文档")
    document = ElementTree.fromstring(
        xml, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    if _name(document) != "document":
        raise ValueError("DOCX 正文结构无效")
    body = next((child for child in document if _name(child) == "body"), None)
    if body is None:
        raise ValueError("DOCX 缺少正文")
    # Bound recursion even for intentionally pathological nested tables/content controls.
    pending = [(body, 0)]
    while pending:
        element, depth = pending.pop()
        if depth > 64:
            raise ValueError("文档结构嵌套过深")
        pending.extend((child, depth + 1) for child in element)
    return "\n".join(_blocks(body))


def _inline(element: Element) -> str:
    name = _name(element)
    if name in {"del", "moveFrom", "instrText", "delText"}:
        return ""
    if name == "t":
        return element.text or ""
    if name in {"br", "cr"}:
        return "\n"
    if name == "tab":
        return "\t"
    if name == "noBreakHyphen":
        return "\u2011"
    if name == "softHyphen":
        return "\u00ad"
    return "".join(_inline(child) for child in element)


def _blocks(element: Element) -> list[str]:
    lines: list[str] = []
    for child in element:
        name = _name(child)
        if name in {"del", "moveFrom"}:
            continue
        if name == "p":
            lines.append(_inline(child))
        elif name == "tr":
            # Cell paragraphs remain in order; tabs separate columns, newlines rows.
            lines.append(
                "\t".join("\n".join(_blocks(cell)) for cell in child if _name(cell) == "tc")
            )
        elif name == "altChunk":
            raise ValueError("正文含尚未转换的嵌入文档")
        else:
            lines.extend(_blocks(child))
    return lines


def _u16(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", data, offset)[0])


def _u32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", data, offset)[0])


def _read_doc(raw: bytes) -> str:
    if not raw.startswith(OLE_SIGNATURE):
        raise ValueError("不是 Word 97–2003 二进制 DOC（不支持改后缀的 RTF/HTML）")
    with olefile.OleFileIO(io.BytesIO(raw), raise_defects=olefile.DEFECT_INCORRECT) as compound:
        if compound.exists("EncryptedPackage"):
            raise ValueError("文档已加密")

        def stream(name: str) -> bytes:
            if not compound.exists(name):
                raise ValueError("DOC 缺少必要的正文数据")
            if compound.get_size(name) > MAX_WORD_BYTES:
                raise ValueError("DOC 内部数据过大")
            return bytes(compound.openstream(name).read())

        word = stream("WordDocument")
        if _u16(word, 0) != 0xA5EC or _u16(word, 2) not in {0xC1, 0xD9, 0x101, 0x10C, 0x112}:
            raise ValueError("暂不支持此旧版 DOC，请转换为 DOCX")
        flags = _u16(word, 10)
        if flags & (0x0100 | 0x8000):
            raise ValueError("文档已加密或使用密码混淆保护")
        table = stream("1Table" if flags & 0x0200 else "0Table")
        # Variable-length FIB arrays, not fixed offsets tied to one Word release.
        lw = 34 + _u16(word, 32) * 2
        if _u16(word, lw) < 4:
            raise ValueError("DOC 文件头不完整")
        main_chars = _u32(word, lw + 2 + 3 * 4)
        if main_chars > MAX_TEXT_CHARS:
            raise ValueError("DOC 正文过长，请拆分文档")
        fc_lcb = lw + 2 + _u16(word, lw) * 4
        if _u16(word, fc_lcb) <= 33:
            raise ValueError("DOC 缺少文字索引")
        offset = _u32(word, fc_lcb + 2 + 33 * 8)
        size = _u32(word, fc_lcb + 2 + 33 * 8 + 4)
        if not size or offset + size > len(table):
            raise ValueError("DOC 文字索引损坏")
        clx = table[offset : offset + size]
        pos = 0
        while pos < len(clx) and clx[pos] == 1:  # optional property records (PrC)
            pos += 3 + _u16(clx, pos + 1)
        if pos + 5 > len(clx) or clx[pos] != 2:
            raise ValueError("DOC 文字索引无效")
        plc_size = _u32(clx, pos + 1)
        plc = clx[pos + 5 : pos + 5 + plc_size]
        if len(plc) != plc_size or plc_size < 16 or (plc_size - 4) % 12:
            raise ValueError("DOC 文字分段索引损坏")
        count = (plc_size - 4) // 12
        cps = [_u32(plc, i * 4) for i in range(count + 1)]
        if cps[0] != 0 or cps[-1] < main_chars or any(a > b for a, b in pairwise(cps)):
            raise ValueError("DOC 文字位置无效")
        pieces: list[bytes] = []
        for i in range(count):
            if cps[i] >= main_chars:
                break
            chars = min(cps[i + 1], main_chars) - cps[i]
            fc = _u32(plc, (count + 1) * 4 + i * 8 + 2)
            compressed = bool(fc & 0x40000000)
            offset = fc & 0x3FFFFFFF
            if compressed:
                offset //= 2
            size = chars * (1 if compressed else 2)
            if offset + size > len(word):
                raise ValueError("DOC 正文分段超出文件范围")
            piece = word[offset : offset + size]
            # MS-DOC compressed characters use the Windows-1252 mapping; Unicode
            # pieces cover Chinese and other non-Latin text, including surrogate pairs.
            pieces.append(piece.decode("cp1252").encode("utf-16-le") if compressed else piece)
    # Decode after joining: a valid surrogate pair can cross a piece boundary.
    return _clean_doc_text(b"".join(pieces).decode("utf-16-le"))


def _clean_doc_text(text: str) -> str:
    # Keep field results, never the internal instructions (e.g. HYPERLINK targets).
    fields: list[bool] = []
    hidden_fields = 0
    output: list[str] = []
    for char in text:
        if char == "\x13":
            fields.append(False)
            hidden_fields += 1
        elif char == "\x14" and fields:
            if not fields[-1]:
                hidden_fields -= 1
            fields[-1] = True
        elif char == "\x15" and fields:
            if not fields.pop():
                hidden_fields -= 1
        elif not hidden_fields:
            output.append(char)
    text = "".join(output).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x07\x07", "\n").replace("\x07", "\t")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n")
    # Embedded-object markers and layout controls are not body text.
    return "".join(char for char in text if char >= " " or char in "\n\t")
