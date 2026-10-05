"""单文件 HTML 导出（Phase 8 / 设计计划 §十四.2）。

逐条落地：

- **只导出当前分支**——不把整个会话树混在一起；
- **单文件 HTML**：样式内联在 ``<style>`` 里，无外部依赖，双击即开；
- **图片内嵌**：从加密 blob 仓读出原图，base64 成 ``data:`` URI；
- **工具步骤可折叠**：``<details>`` 元素，默认收起；
- **保留模型和站点信息**：每条助手消息标出逻辑模型 → 端点与路由原因；
- **默认不含完整请求头、密钥、原始请求日志**：只导出**摘要**
  （模型/端点/参数键/状态码），请求体与请求头一律不导出；
- **导出前显示隐私提示**：:func:`privacy_notice` 返回提示文本，UI 必须先展示；
- **导出文件不是加密文件**：明文 HTML——这是刻意的，也是必须在提示里说明的。

导出内容里的消息正文来自应用权威数据（已解密），因此**导出文件本身是明文**。
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from limbowave.application.branch_path import branch_messages
from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.domain.conversation import Message
from limbowave.domain.run import RunRecord
from limbowave.infrastructure.crypto.blob_store import BlobStore

PRIVACY_NOTICE = (
    "导出的文件是**明文文件**，不加密。\n"
    "它包含：会话消息正文、模型的思考内容（如有）、以及每条回复使用的模型与站点。\n"
    "它**不含**：API 密钥、完整请求头、原始请求/响应载荷、权限审计记录。\n"
    "HTML 还会内嵌图片与工具步骤。消息正文中自行粘贴的敏感信息不会自动脱敏。\n"
    "请把导出文件放在你认为安全的位置；发给他人前请自行确认内容。"
)


def privacy_notice() -> str:
    """导出前的隐私提示文本（§十四.2：导出前显示隐私提示）。"""
    return PRIVACY_NOTICE


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    message_count: int
    image_count: int
    bytes_written: int


def _highlight(code: str, lang: str | None, _attrs: str | None) -> str:
    lexer = None
    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = None
    if lexer is None:
        return html.escape(code)
    # noclasses=True：内联样式。单文件导出不能依赖外部 CSS 文件，
    # 而 pygments 的 class 模式需要一份样式表——那样导出就不是"单文件"了
    formatter = HtmlFormatter(noclasses=True, nowrap=True, style="monokai")
    return highlight(code, lexer, formatter).rstrip("\n")


_MD = MarkdownIt("commonmark", {"highlight": _highlight, "html": False})
_MD.enable("table")
_MD.enable("strikethrough")


class ExportService:
    """把一条分支导出成单文件 HTML。只读，不改任何数据。"""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, blob_store: BlobStore | None,
        *, model_names: Mapping[str, str] | None = None,
        endpoint_names: Mapping[str, str] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._blobs = blob_store
        self._model_names = dict(model_names or {})
        self._endpoint_names = dict(endpoint_names or {})

    def _display_reason(self, reason: str) -> str:
        """只转换溯源说明，不替换消息正文；一次替换避免显示名被二次替换。"""
        names = {**self._model_names, **self._endpoint_names}
        names = {key: value for key, value in names.items() if key and value}
        if not names:
            return reason
        alternatives = "|".join(re.escape(key) for key in sorted(names, key=len, reverse=True))
        return re.sub(
            rf"(?<![\w./:-])(?:{alternatives})(?![\w./:-])",
            lambda match: names[match.group()], reason,
        )

    def render_plain(self, branch_id: str, fmt: str) -> str:
        """把分支渲染成 md / txt / json 三种纯文本格式（导出悬浮窗用）。

        与 HTML 导出同源同数据（同一 uow），但不内嵌图片二进制——
        图片只给一行标注（原图留在资料库里，不入文本文件）。
        """
        fmt = fmt.lower()
        if fmt not in ("md", "txt", "json"):
            raise ValueError(f"不支持的文本导出格式：{fmt}")
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None:
                raise ValueError(f"分支不存在：{branch_id}")
            conversation = uow.conversations.get(branch.conversation_id)
            title = conversation.title if conversation else "未命名会话"
            messages = branch_messages(uow, branch_id)
            runs = {
                r.user_message_id: r
                for r in uow.runs.list_for_conversation(branch.conversation_id)
            }
            rows: list[dict[str, object]] = []
            for message in messages:
                run = runs.get(message.id)
                rows.append(
                    {
                        "role": message.role.value,
                        "content": message.content,
                        "thinking": message.thinking or "",
                        "created_at": message.created_at.astimezone().isoformat(),
                        "run_id": run.id if run else None,
                    }
                )
            header = f"# {title}" if fmt == "md" else title

        if fmt == "json":
            import json as _json

            return _json.dumps(
                {"title": title, "message_count": len(rows), "messages": rows},
                ensure_ascii=False,
                indent=2,
            )
        parts = [header, ""]
        for index, row in enumerate(rows, 1):
            who = "用户" if row["role"] == "user" else "助手"
            stamp = str(row["created_at"])
            if fmt == "md":
                parts.append(f"## {index}. {who}")
                parts.append("")
                parts.append(f"_{stamp}_")
                parts.append("")
                parts.append(str(row["content"]))
                if row["thinking"]:
                    parts.append("")
                    parts.append("<details><summary>思考过程</summary>")
                    parts.append("")
                    parts.append(str(row["thinking"]))
                    parts.append("")
                    parts.append("</details>")
            else:
                parts.append(f"[{index}] {who}  {stamp}")
                parts.append(str(row["content"]))
                if row["thinking"]:
                    parts.append(f"（思考过程）{row['thinking']}")
            parts.append("")
            parts.append("---" if fmt == "md" else "=" * 40)
            parts.append("")
        return chr(10).join(parts)

    def export_branch(self, branch_id: str, target: Path) -> ExportResult:
        """导出指定分支。图片内嵌、工具步骤折叠、模型信息保留。"""
        with self._uow_factory() as uow:
            page, stats = self._render(uow, branch_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        return ExportResult(
            path=target,
            message_count=stats["messages"],
            image_count=stats["images"],
            bytes_written=len(page.encode("utf-8")),
        )

    def export_selection(self, branch_ids: list[str], fmt: str, target: Path) -> list[Path]:
        """每个分支独立导出；排他创建文件，绝不静默覆盖已有内容。"""
        if fmt not in {"html", "md", "txt", "json"}:
            raise ValueError(f"不支持的导出格式：{fmt}")
        ids = list(dict.fromkeys(branch_ids))
        if not ids:
            raise ValueError("请至少选择一个分支")
        suffix = f".{fmt}"
        stem = target.name[:-len(suffix)] if target.name.lower().endswith(suffix) else target.name
        outputs = [
            target.with_name(f"{stem}{f'-{index:03d}' if len(ids) > 1 else ''}{suffix}")
            for index in range(1, len(ids) + 1)
        ]
        # 先校验并渲染全部选择，避免无效分支导致只导出前半部分。
        for path in outputs:
            if path.exists():
                raise FileExistsError(f"文件已存在，请更换文件名：{path}")
        pages: list[str] = []
        for branch_id in ids:
            if fmt == "html":
                with self._uow_factory() as uow:
                    page, _stats = self._render(uow, branch_id)
            else:
                page = self.render_plain(branch_id, fmt)
            pages.append(page)
        target.parent.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        try:
            for path, page in zip(outputs, pages, strict=True):
                with path.open("x", encoding="utf-8") as stream:
                    created.append(path)
                    stream.write(page)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return outputs

    # ---------- 渲染 ----------

    def _render(self, uow: UnitOfWork, branch_id: str) -> tuple[str, dict[str, int]]:
        branch = uow.branches.get(branch_id)
        if branch is None:
            raise ValueError(f"分支不存在：{branch_id}")
        conversation = uow.conversations.get(branch.conversation_id)
        title = conversation.title if conversation else "未命名会话"
        messages = branch_messages(uow, branch_id)
        runs = {
            r.user_message_id: r for r in uow.runs.list_for_conversation(branch.conversation_id)
        }
        run_by_assistant = {
            r.assistant_message_id: r
            for r in uow.runs.list_for_conversation(branch.conversation_id)
            if r.assistant_message_id
        }

        body_parts: list[str] = []
        counted_runs: set[str] = set()
        images_embedded = 0
        for message in messages:
            run = runs.get(message.id) or run_by_assistant.get(message.id)
            body_parts.append(self._render_message(uow, message, run))
            # 图片按 run 去重计数（一个 run 的两条消息不是两张图）
            if run is not None and message.role.value == "user":
                run_id = run.id
                if run_id not in counted_runs:
                    counted_runs.add(run_id)
                    images_embedded += self._count_images(uow, run)

        page = _PAGE_TEMPLATE.format(
            title=html.escape(title),
            branch_note=("（分叉分支）" if branch.forked_from_message_id else ""),
            exported_at=datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            message_count=len(messages),
            body="\n".join(body_parts),
            css=_CSS,
        )
        return page, {"messages": len(messages), "images": images_embedded}

    def _render_message(self, uow: UnitOfWork, message: Message, run: RunRecord | None) -> str:
        role = message.role.value
        content = message.content
        thinking = message.thinking
        message_id = message.id
        label = "用户" if role == "user" else "助手"

        parts = [f'<article class="msg {role}" id="{html.escape(message_id)}">']
        parts.append(f'<div class="role">{label}</div>')
        if thinking:
            # 思考内容默认折叠（与 UI 一致）
            parts.append(
                '<details class="thinking"><summary>思考过程</summary>'
                f"<pre>{html.escape(thinking)}</pre></details>"
            )
        parts.append(f'<div class="content">{_MD.render(content)}</div>')
        if run is not None:
            # 图片挂在**用户**消息下（附件是那一轮用户带的）；溯源信息挂在**助手**
            # 消息下。两者分开渲染——否则同一个 run 的两条消息会把图片嵌两遍。
            if role == "user":
                images = self._render_images(uow, run)
                if images:
                    parts.append(images)
            else:
                parts.extend(self._render_provenance(uow, run))
        parts.append("</article>")
        return "\n".join(parts)

    def _render_images(self, uow: UnitOfWork, run: RunRecord) -> str:
        """把该轮的图片附件内嵌成 data URI（§十四.2：图片以内嵌资源方式保存）。"""
        if self._blobs is None:
            return ""
        intent = uow.snapshots.get_intent(run.id)
        if intent is None or not intent.attachment_ids:
            return ""
        mimes = {"jpeg": "image/jpeg", "png": "image/png"}
        chunks: list[str] = []
        for attachment_id in intent.attachment_ids:
            attachment = uow.image_attachments.get(attachment_id)
            if attachment is None:
                continue  # 文本/剪贴板附件不是图片，跳过
            try:
                raw = self._blobs.get(attachment.blob_id)
            except Exception:
                continue  # blob 缺失就跳过，不让导出整体失败
            mime = mimes.get(attachment.format.value, "application/octet-stream")
            caption = (
                f"{attachment.format.value.upper()} · {attachment.dimensions}"
                f" · {attachment.size_bytes} 字节"
            )
            chunks.append(
                f'<figure><img src="{_image_data_uri(raw, mime)}" '
                f'alt="{html.escape(attachment.source_path or attachment.id)}">'
                f"<figcaption>{caption}</figcaption></figure>"
            )
        return f'<div class="attachments">{"".join(chunks)}</div>' if chunks else ""

    def _render_provenance(self, uow: UnitOfWork, run: RunRecord) -> list[str]:
        """模型/站点信息 + 工具步骤（折叠）。**不含请求头、密钥、原始载荷**。"""
        run_id = run.id
        intent = uow.snapshots.get_intent(run_id)
        transports = uow.snapshots.list_transport(run_id)
        out: list[str] = []
        if intent is not None:
            model_name = self._model_names.get(intent.logical_model_id) or intent.logical_model_id
            endpoint_name = self._endpoint_names.get(intent.endpoint_id) or intent.endpoint_id
            reason = self._display_reason(intent.routing_reason)
            out.append(
                '<div class="provenance">模型 '
                f"<code>{html.escape(model_name)}</code> → 站点 "
                f"<code>{html.escape(endpoint_name)}</code>"
                f'<span class="reason">（{html.escape(reason)}）</span></div>'
            )
        if transports:
            # 工具步骤 / 传输摘要：只给状态与序号，不给请求体与请求头
            rows = "".join(
                f"<li>第 {t.sequence} 次传输 · HTTP {t.response_status or '—'}"
                f"{' · ' + html.escape(t.stop_reason) if t.stop_reason else ''}</li>"
                for t in transports
            )
            out.append(
                f'<details class="toolsteps"><summary>工具步骤（{len(transports)}）</summary>'
                f"<ul>{rows}</ul>"
                '<p class="note">已省略完整请求头与请求体：导出不含密钥或原始载荷。</p>'
                "</details>"
            )
        return out

    def _count_images(self, uow: UnitOfWork, run: RunRecord | None) -> int:
        """该轮实际内嵌的图片数（只算真的找到了登记卡与 blob 的）。"""
        if run is None or self._blobs is None:
            return 0
        intent = uow.snapshots.get_intent(run.id)
        if intent is None:
            return 0
        count = 0
        for attachment_id in intent.attachment_ids:
            attachment = uow.image_attachments.get(attachment_id)
            if attachment is not None and self._blobs.exists(attachment.blob_id):
                count += 1
        return count


def _image_data_uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


_CSS = """
:root { color-scheme: dark; }
body { margin: 0; background: #0f1115; color: #e6e9ef;
  font-family: 'Segoe UI', 'Microsoft YaHei UI', system-ui, sans-serif; }
main { max-width: 840px; margin: 0 auto; padding: 32px 20px 64px; }
header { border-bottom: 1px solid #2a2f3a; padding-bottom: 16px; margin-bottom: 24px; }
h1 { font-size: 20px; margin: 0 0 6px; }
.meta { color: #9aa3b2; font-size: 12px; }
.notice { margin-top: 12px; padding: 10px 12px; border: 1px solid #2a2f3a;
  border-radius: 8px; color: #9aa3b2; font-size: 12px; white-space: pre-line; }
.msg { margin: 0 0 18px; padding: 12px 14px; border-radius: 12px; background: #171a21; }
.msg.user { background: #1d2340; }
.role { font-size: 11px; color: #9aa3b2; margin-bottom: 6px; }
.content > :first-child { margin-top: 0; }
.content pre { background: #1c1f27; padding: 12px; border-radius: 8px; overflow-x: auto; }
.content code { font-family: 'Cascadia Code', Consolas, monospace; font-size: 13px; }
.content :not(pre) > code { background: #1c1f27; padding: 1px 5px; border-radius: 4px; }
.content table { border-collapse: collapse; }
.content td, .content th { border: 1px solid #2a2f3a; padding: 6px 10px; }
.content a { color: #5b6cff; }
details { margin-top: 10px; }
summary { cursor: pointer; color: #9aa3b2; font-size: 12px; }
details.thinking pre { white-space: pre-wrap; color: #9aa3b2; font-size: 12px; }
.provenance { margin-top: 10px; font-size: 12px; color: #9aa3b2; }
.provenance code { color: #e6e9ef; }
.reason { opacity: .8; }
.toolsteps ul { margin: 8px 0; padding-left: 20px; font-size: 12px; color: #9aa3b2; }
.note { font-size: 11px; color: #6f7788; }
img { max-width: 100%; border-radius: 8px; }
"""

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — LimboWave 导出</title>
<style>{css}</style>
</head>
<body>
<main>
<header>
  <h1>{title}{branch_note}</h1>
  <div class="meta">导出时间 {exported_at} · {message_count} 条消息 · 仅当前分支</div>
  <div class="notice">本文件是明文，不含密钥、完整请求头与原始请求日志。</div>
</header>
{body}
</main>
</body>
</html>
"""
