"""附件装配服务：把附件 ID 变成「发送给内核的东西」。

这是 UI 与内核之间的接缝（Phase 4）：

- **图片**：从加密 blob 仓读**原图字节**，base64 编码成内核载荷
  （``{type:"image", data, mimeType}``，合同 §8.4）。原图直传，不压缩。
- **文本/剪贴板文档**：**不内联进 prompt**——模型通过 ``read_document`` 工具按
  ``file_id`` 按需读（设计计划 §九.1 的内置工具模式 / §八.2 精确读取）。这里把
  附件 id 透传给意图快照，并生成 ``document_note``——**只列附件清单**
  （file_id / 名称 / 行数），不列内容：模型不知道有文件可读，工具再好也读不到。

这个区分是有意的：图片必须随消息走（模型没有"读图工具"），文本文件
交给按需读取（避免把整份文件塞进上下文）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from limbowave.application.services.file_service import FileService
from limbowave.application.services.image_service import ImageService
from limbowave.domain.files import ImageFormat

_MIME = {ImageFormat.JPEG: "image/jpeg", ImageFormat.PNG: "image/png"}


@dataclass(frozen=True, slots=True)
class AttachmentPayload:
    """一轮发送的附件载荷：内核图片 + 附件 id 列表。"""

    attachment_ids: list[str] = field(default_factory=list)
    images: list[dict[str, object]] = field(default_factory=list)
    # 供 UI 显示的附件名（id → 显示名）
    display_names: dict[str, str] = field(default_factory=dict)
    # 给模型的文本文档清单（只列元信息，不含内容；随 prompt 追加）
    document_note: str = ""


class AttachmentService:
    """把一组附件 ID 装配成发送载荷。"""

    def __init__(self, files: FileService, images: ImageService) -> None:
        self._files = files
        self._images = images

    def build(self, attachment_ids: list[str]) -> AttachmentPayload:
        """装配。未知 id 跳过（调用方应只传有效 id）；图片进载荷，文本只记 id。"""
        images: list[dict[str, object]] = []
        names: dict[str, str] = {}
        valid_ids: list[str] = []
        documents: list[str] = []
        for attachment_id in attachment_ids:
            # 先试图片
            image = self._images.get(attachment_id)
            if image is not None:
                raw = self._images.read_bytes(image.id)  # 原图字节
                images.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(raw).decode("ascii"),
                        "mimeType": _MIME[image.format],
                    }
                )
                names[attachment_id] = image.source_path or image.id
                valid_ids.append(attachment_id)
                continue
            # 再试文本/剪贴板文档
            document = self._files.get(attachment_id)
            if document is not None:
                names[attachment_id] = document.display_name
                valid_ids.append(attachment_id)
                location = f"路径 {document.path} · " if document.path else ""
                documents.append(
                    f"- file_id: {document.id} · 名称: {document.display_name}"
                    f" · {location}{document.line_count} 行"
                )
        return AttachmentPayload(
            attachment_ids=valid_ids,
            images=images,
            display_names=names,
            document_note=_compose_document_note(documents),
        )


def _compose_document_note(document_lines: list[str]) -> str:
    """给模型的附件清单。只列元信息——内容由模型用 read_document 按需读取。"""
    if not document_lines:
        return ""
    return (
        "[用户在本条消息附加了以下文本文档。"
        "需要内容时用 read_document 工具按 file_id 读取：行号从 1 开始，"
        "start_line/end_line 为闭区间，单次默认最多 200 行，长文件请分段读取。]\n"
        + "\n".join(document_lines)
    )
