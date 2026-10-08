"""精确读取工具的重点测试矩阵（Task 4.2 的九条）+ 索引（Task 4.1）。

九条重点：
1. 第 1 行
2. 文件末尾
3. 空文件
4. 少于申请范围（申请超尾部收敛到真实行数）
5. 超过 200 行（默认上限拒绝 + 建议分段）
6. 用户调整上限（会话覆盖）
7. 文件被修改（变化检测 + 重索引）
8. 超长单行（截断 + 如实标记）
9. 中文与混合换行符（\\n / \\r\\n / \\r）

另加：行号从 1 开始、不擅自截取、编码检测、不支持的类型明确拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.file_service import (
    FileReadError,
    FileService,
    InvalidRange,
    RangeLimitExceeded,
)
from limbowave.domain.files import UnsupportedFileType, detect_encoding
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


@pytest.fixture
def service() -> FileService:
    return FileService(in_memory_uow_factory(InMemoryStore()))


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _write_lines(path: Path, n: int, sep: bytes = b"\n") -> Path:
    return _write(path, sep.join(f"第{i}行".encode() for i in range(1, n + 1)))


# ---------- 1. 第 1 行 ----------


def test_first_line(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "a.txt", 10)
    doc = service.index_path(path)
    result = service.read(doc.id, 1, 1)
    assert result.actual_range == (1, 1)
    assert result.text == "第1行"
    assert result.total_lines == 10


# ---------- 2. 文件末尾 ----------


def test_last_lines(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "a.txt", 10)
    doc = service.index_path(path)
    result = service.read(doc.id, 9, 10)
    assert result.actual_range == (9, 10)
    assert result.text == "第9行\n第10行"


def test_read_past_end_clamps(service: FileService, tmp_path: Path) -> None:
    """申请范围超出文件末尾：实际范围收敛到真实行数，不报错。"""
    path = _write_lines(tmp_path / "a.txt", 5)
    doc = service.index_path(path)
    result = service.read(doc.id, 4, 100)  # 文件只有 5 行
    assert result.actual_range == (4, 5)
    assert result.text == "第4行\n第5行"
    assert result.requested_range == (4, 100)  # 申请范围如实记录


# ---------- 3. 空文件 ----------


def test_empty_file(service: FileService, tmp_path: Path) -> None:
    path = _write(tmp_path / "empty.txt", b"")
    doc = service.index_path(path)
    assert doc.line_count == 0
    result = service.read(doc.id, 1, 200)
    assert result.text == ""
    assert result.total_lines == 0
    assert result.actual_range == (1, 0)  # 空区间如实表达


# ---------- 4. 少于申请范围 ----------


def test_fewer_lines_than_requested(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "a.txt", 3)
    doc = service.index_path(path)
    result = service.read(doc.id, 1, 200)
    assert result.actual_range == (1, 3)
    assert result.total_lines == 3


# ---------- 5. 超过 200 行（默认上限） ----------


def test_default_limit_rejects_over_200(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "big.txt", 500)
    doc = service.index_path(path)
    assert doc.line_count == 500
    with pytest.raises(RangeLimitExceeded) as exc_info:
        service.read(doc.id, 1, 201)
    exc = exc_info.value
    assert exc.limit == 200
    assert exc.requested == (1, 201)
    assert exc.suggested_segments == [(1, 200), (201, 201)]  # 建议分段
    # 不擅自截取：调用失败，没有任何内容返回
    assert "201" in str(exc) and "200" in str(exc)


def test_exactly_200_allowed(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "big.txt", 300)
    doc = service.index_path(path)
    result = service.read(doc.id, 1, 200)
    assert result.actual_range == (1, 200)
    assert len(result.text.splitlines()) == 200


# ---------- 6. 用户调整上限 ----------


def test_session_override_limit(service: FileService, tmp_path: Path) -> None:
    """会话级覆盖上限：500 行一次读完。"""
    path = _write_lines(tmp_path / "big.txt", 500)
    doc = service.index_path(path)
    result = service.read(doc.id, 1, 500, session_max_lines=500)
    assert result.actual_range == (1, 500)
    assert len(result.text.splitlines()) == 500


def test_session_override_stricter(service: FileService, tmp_path: Path) -> None:
    """会话也可以把上限调得更严。"""
    path = _write_lines(tmp_path / "a.txt", 50)
    doc = service.index_path(path)
    with pytest.raises(RangeLimitExceeded) as exc_info:
        service.read(doc.id, 1, 10, session_max_lines=5)
    assert exc_info.value.limit == 5


# ---------- 7. 文件被修改 ----------


def test_file_changed_reindexes_and_marks(service: FileService, tmp_path: Path) -> None:
    """文件修改后：重新索引（新行数新哈希），读取结果标记 changed_since_index。"""
    path = _write_lines(tmp_path / "a.txt", 10)
    doc = service.index_path(path)
    old_hash = doc.content_hash

    # 修改文件：加 5 行
    path.write_bytes(b"\n".join(f"新{i}".encode() for i in range(1, 16)))

    result = service.read(doc.id, 1, 10)
    assert result.changed_since_index is True
    assert result.total_lines == 15  # 新行数
    assert result.content_hash != old_hash  # 新哈希
    assert "新1" in result.text


def test_read_record_pins_old_hash(service: FileService, tmp_path: Path) -> None:
    """修改前的读取记录仍带旧哈希——版本不一致可判定。"""
    path = _write_lines(tmp_path / "a.txt", 5)
    doc = service.index_path(path)
    before = service.read(doc.id, 1, 5)
    path.write_bytes(b"completely different")
    after = service.read(doc.id, 1, 5)
    assert before.content_hash != after.content_hash
    assert before.changed_since_index is False
    assert after.changed_since_index is True


# ---------- 8. 超长单行 ----------


def test_very_long_single_line_truncated(service: FileService, tmp_path: Path) -> None:
    """超长单行截断并如实标记行号，不静默吞掉。"""
    long_line = "x" * 300_000  # 超过 MAX_LINE_BYTES (256KiB)
    path = _write(tmp_path / "long.txt", f"短行\n{long_line}\n又一行".encode())
    doc = service.index_path(path)
    result = service.read(doc.id, 1, 3)
    assert result.truncated_lines == (2,)  # 第 2 行被截断
    assert "短行" in result.text
    assert "又一行" in result.text


# ---------- 9. 中文与混合换行符 ----------


def test_chinese_and_mixed_line_endings(service: FileService, tmp_path: Path) -> None:
    """\\n、\\r\\n、\\r 混合 + 中文：行号稳定，内容不丢字。"""
    content = "第一行\n第二行\r\n第三行\r第四行".encode()
    path = _write(tmp_path / "mixed.txt", content)
    doc = service.index_path(path)
    assert doc.line_count == 4

    result = service.read(doc.id, 2, 3)
    assert result.text == "第二行\n第三行"  # 换行符归一为 \n，行内容完整


def test_crlf_offsets_stable(service: FileService, tmp_path: Path) -> None:
    path = _write(tmp_path / "crlf.txt", b"alpha\r\nbeta\r\ngamma")
    doc = service.index_path(path)
    assert service.read(doc.id, 1, 1).text == "alpha"
    assert service.read(doc.id, 3, 3).text == "gamma"


# ---------- 编码检测（Task 4.1） ----------


@pytest.mark.parametrize(
    ("raw", "expected_encoding", "expected_text"),
    [
        ("中文 UTF-8".encode(), "utf-8", "中文 UTF-8"),
        (b"\xef\xbb\xbf" + "带 BOM".encode(), "utf-8-sig", "带 BOM"),
        ("中文 GBK".encode("gbk"), "gbk", "中文 GBK"),
        (b"\xff\xfe" + "十六".encode("utf-16-le"), "utf-16-le", "十六"),
        (b"\xfe\xff" + "十六".encode("utf-16-be"), "utf-16-be", "十六"),
    ],
)
def test_encoding_detection(raw: bytes, expected_encoding: str, expected_text: str) -> None:
    encoding, text = detect_encoding(raw)
    assert encoding == expected_encoding
    assert text == expected_text


def test_gbk_file_indexes_and_reads(service: FileService, tmp_path: Path) -> None:
    path = _write(tmp_path / "gbk.txt", "中文标题\n第二行".encode("gbk"))
    doc = service.index_path(path)
    assert doc.encoding == "gbk"
    result = service.read(doc.id, 1, 2)
    assert "中文标题" in result.text


# ---------- 行号从 1 开始 / 不擅自截取 / 类型拒绝 ----------


def test_line_zero_rejected(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "a.txt", 5)
    doc = service.index_path(path)
    with pytest.raises(InvalidRange):
        service.read(doc.id, 0, 1)


def test_end_before_start_rejected(service: FileService, tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "a.txt", 5)
    doc = service.index_path(path)
    with pytest.raises(InvalidRange):
        service.read(doc.id, 5, 2)


def test_pdf_explicitly_rejected(service: FileService, tmp_path: Path) -> None:
    """PDF 明确标记为不支持，而不是错误解析。"""
    path = _write(tmp_path / "doc.pdf", b"%PDF fake pdf")
    with pytest.raises(UnsupportedFileType, match="不支持"):
        service.index_path(path)


def test_reindex_same_path_keeps_stable_id(service: FileService, tmp_path: Path) -> None:
    """重复索引同一路径：稳定 id 不变（登记卡更新而非新建）。"""
    path = _write_lines(tmp_path / "a.txt", 5)
    first = service.index_path(path)
    path.write_bytes(b"changed content")
    second = service.index_path(path)
    assert first.id == second.id
    assert second.content_hash != first.content_hash


def test_read_unknown_document(service: FileService) -> None:
    with pytest.raises(FileReadError, match="不存在"):
        service.read("no-such-id", 1, 1)


def test_no_partial_read_on_limit(service: FileService, tmp_path: Path) -> None:
    """超过上限时**不**返回前 200 行——调用整体失败。"""
    path = _write_lines(tmp_path / "big.txt", 500)
    doc = service.index_path(path)
    with pytest.raises(RangeLimitExceeded):
        service.read(doc.id, 1, 500)  # 默认上限 200
    # 没有"默默给你前 200 行"这种事
    with pytest.raises(RangeLimitExceeded):
        service.read(doc.id, 1, 201)
