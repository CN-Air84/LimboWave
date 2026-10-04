"""工具网关与内置工具（Phase 6 / 设计计划 §九）。

统一网关流水线（§9.2，顺序固定）：

    参数模式校验 → 权限与资源范围检查 → 高影响操作检查
    → 执行 → 输出限制与敏感信息处理 → 审计记录 → 结构化结果返回

内置工具（§9.1）：

- 附件：``read_document``（按 ``file_id`` 精确读取用户登记的 TXT/MD/剪贴板文档，§8.2）
- 文件：``list_directory`` / ``stat_file`` / ``search_text`` / ``create_file`` / ``modify_file``
- 联网：``read_url``（``web_search`` 需要搜索服务凭据——未配置时**如实返回
  「未配置」结构化错误，不伪造结果**）

所有路径经 :mod:`limbowave.domain.path_guard` 校验（解析符号链接后再判包含），
所有网络目标经 :mod:`limbowave.domain.network_guard` 校验。
**网关是唯一入口**——工具实现不自己做权限判断，避免出现绕过路径。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from limbowave.application.services.file_service import (
    FileReadError,
    FileService,
    RangeLimitExceeded,
)
from limbowave.application.services.memory_service import MemoryService
from limbowave.application.services.permission_service import PermissionService
from limbowave.domain.memory import MemoryRunContext
from limbowave.domain.network_guard import NetworkBlocked, check_host
from limbowave.domain.path_guard import PathEscape, resolve_within
from limbowave.domain.permissions import (
    Capability,
    Decision,
    ExecutionMode,
    ResourceScope,
    ToolRequest,
)
from limbowave.domain.shell import ShellResult
from limbowave.domain.shell_diagnostics import detect_bash_isms

# 输出上限：超过就截断并**如实标记**（不静默丢内容）
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_SEARCH_MAX_RESULTS = 100


class ToolError(Exception):
    """工具执行失败的基类。结构化返回给模型。"""


class InvalidParams(ToolError):
    """参数模式校验失败。"""


class ToolDenied(ToolError):
    """权限网关拒绝。"""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """结构化工具结果。``truncated`` 与 ``notes`` 让模型知道结果是否完整。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    truncated: bool = False
    notes: tuple[str, ...] = ()


# ---------- 参数模式（§9.2 第一步） ----------

# 每个工具的参数签名：(参数名, 必填, 类型)
_SCHEMAS: dict[str, tuple[tuple[str, bool, type], ...]] = {
    "add_session_memory": (("content", True, str), ("call_id", True, str), ("run_id", True, str)),
    "read_document": (("file_id", True, str), ("start_line", False, int), ("end_line", False, int)),
    "list_directory": (("path", False, str),),
    "stat_file": (("path", True, str),),
    "search_text": (("path", False, str), ("pattern", True, str), ("max_results", False, int)),
    "create_file": (("path", True, str), ("content", True, str)),
    "modify_file": (("path", True, str), ("old_text", True, str), ("new_text", True, str)),
    "read_url": (("url", True, str),),
    "web_search": (("query", True, str),),
    # 直接终端模式（§9.1）：受控命令执行
    "run_command": (("command", True, str), ("timeout_seconds", False, int)),
}

_CAPABILITY: dict[str, Capability] = {
    "add_session_memory": Capability.MEMORY_WRITE,
    "read_document": Capability.FILE_READ,
    "list_directory": Capability.FILE_READ,
    "stat_file": Capability.FILE_READ,
    "search_text": Capability.FILE_READ,
    "create_file": Capability.FILE_WRITE,
    "modify_file": Capability.FILE_WRITE,
    "read_url": Capability.NETWORK,
    "web_search": Capability.NETWORK,
    "run_command": Capability.TERMINAL,
}


def capability_of(tool_name: str) -> Capability:
    """工具对应的能力。未知工具抛 InvalidParams。模块级——权限判定与网关共用。"""
    capability = _CAPABILITY.get(tool_name)
    if capability is None:
        raise InvalidParams(f"未知工具：{tool_name}")
    return capability


def validate_params(tool_name: str, params: dict[str, Any]) -> None:
    """按签名校验参数。缺必填、类型不符、未知参数都抛 InvalidParams。"""
    schema = _SCHEMAS.get(tool_name)
    if schema is None:
        raise InvalidParams(f"未知工具：{tool_name}")
    known = {name for name, _, _ in schema}
    unknown = set(params) - known
    if unknown:
        raise InvalidParams(f"未知参数：{', '.join(sorted(unknown))}")
    for name, required, expected in schema:
        if name not in params:
            if required:
                raise InvalidParams(f"缺少必填参数：{name}")
            continue
        value = params[name]
        if not isinstance(value, expected):
            raise InvalidParams(
                f"参数 {name} 类型不符：期望 {expected.__name__}，得到 {type(value).__name__}"
            )


def normalize_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """去掉取空白串的**可选**字符串参数，让它们回落到默认值。

    模型常把「不填」写成空串（``list_directory {"path": ""}``）。对可选路径那就是
    「用默认目录」；原样交给路径守卫会被当成非法空路径——用户刚点完确认，工具却报
    「路径为空」，模型换个写法重试，又弹一次确认。必填参数不动：空的必填路径照样拒绝。
    """
    blank = {
        name
        for name, required, expected in _SCHEMAS.get(tool_name, ())
        if not required
        and expected is str
        and isinstance(params.get(name), str)
        and not params[name].strip()
    }
    if not blank:
        return params
    return {key: value for key, value in params.items() if key not in blank}


# ---------- 内置工具实现 ----------


class FileTools:
    """文件与目录工具。所有路径都经路径守卫（防越权穿越，Task 6.2）。"""

    def __init__(self, root: Path, documents: FileService | None = None) -> None:
        self._root = root
        self._documents = documents

    def resolve_path(self, raw: str) -> Path:
        """解析并确认路径在工作区内。越界抛 OutsideAllowedRoot。"""
        return resolve_within(self._root, raw)

    def list_directory(self, path: str = ".") -> dict[str, Any]:
        target = self.resolve_path(path)
        if not target.is_dir():
            raise ToolError(f"不是目录：{path}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": stat.st_size if child.is_file() else 0,
                }
            )
        return {"path": str(target), "entries": entries, "count": len(entries)}

    def stat_file(self, path: str) -> dict[str, Any]:
        target = self.resolve_path(path)
        if not target.exists():
            raise ToolError(f"不存在：{path}")
        stat = target.stat()
        return {
            "path": str(target),
            "type": "dir" if target.is_dir() else "file",
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
        }

    def search_text(
        self, pattern: str, path: str = ".", max_results: int = DEFAULT_SEARCH_MAX_RESULTS
    ) -> tuple[dict[str, Any], bool]:
        """文本搜索。返回 (结果, 是否截断)。行号语义与精确读取工具一致。"""
        if not pattern:
            raise InvalidParams("搜索模式为空")
        target = self.resolve_path(path)
        hits: list[dict[str, Any]] = []
        files = (
            [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        )
        truncated = False
        for file_path in files:
            if len(hits) >= max_results:
                truncated = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    hits.append({"file": str(file_path), "line": line_no, "text": line[:200]})
                    if len(hits) >= max_results:
                        truncated = True
                        break
        return {"pattern": pattern, "hits": hits, "count": len(hits)}, truncated

    def create_file(self, path: str, content: str) -> dict[str, Any]:
        """创建文件。已存在则拒绝（避免静默覆盖）。"""
        target = self.resolve_path(path)
        if target.exists():
            raise ToolError(f"文件已存在，不覆盖：{path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    def modify_file(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """精确修改：``old_text`` 必须**恰好出现一次**，否则拒绝（防误改）。"""
        target = self.resolve_path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")
        original = target.read_text(encoding="utf-8")
        count = original.count(old_text)
        if count == 0:
            raise ToolError("未找到要替换的文本")
        if count > 1:
            raise ToolError(f"要替换的文本出现了 {count} 次（要求恰好一次），拒绝修改")
        target.write_text(original.replace(old_text, new_text, 1), encoding="utf-8")
        return {"path": str(target), "replaced": 1}


class ShellRunner(Protocol):
    """shell 执行器的最小接口。infrastructure 的 PowerShellExecutor 满足它。

    用 Protocol 而非具体类：application 层不依赖 infrastructure（架构边界），
    同时保留完整类型检查（不用 ``object`` 丢掉返回类型）。
    """

    def run(self, command: str, *, working_directory: Path | None = None) -> ShellResult: ...


class TerminalTools:
    """受控命令执行（Task 7.4 / §9.1 直接终端模式）。

    执行走 Phase 7 的 PowerShell 执行器：临时 .ps1 + 编码统一 + stdout/stderr
    分离 + 退出码与 PowerShell 错误分别采集。**不做 Bash 误用自动改写**——
    检测到疑似 Bash 语法时返回结构化诊断，让模型自己出修正版（§10.4）。
    """

    def __init__(self, executor: ShellRunner) -> None:
        self._executor = executor

    def run_command(self, command: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        """执行当前后端的 Shell 命令。返回结构化结果（§10.5）。

        ``timeout_seconds`` 由执行器构造时的超时统一控制——逐次覆盖需要执行器
        支持，这里不提供「看似支持实则被忽略」的参数。
        """
        # Bash 误用检测：返回诊断让模型修正，不自动改写后执行
        result = self._executor.run(command)
        diagnostics = (
            detect_bash_isms(command) if result.shell in ("pwsh", "powershell") else ()
        )
        payload = result.to_payload()
        if diagnostics:
            # 结构化诊断附在结果里（不自动改成另一条命令去执行，§10.4）
            payload["bash_syntax_diagnostics"] = [
                {"pattern": d.pattern, "detail": d.detail, "suggestion": d.suggestion}
                for d in diagnostics
            ]
            payload["notes"] = [
                *result.notes,
                "检测到疑似 Bash 语法；当前 shell 是 PowerShell，请修正后重试",
            ]
        return payload


class NetworkTools:
    """联网工具（Task 6.3）。默认禁止环回/私有网段/云元数据。"""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        fetcher: Callable[[str], tuple[int, str, bytes]] | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._max_bytes = max_bytes
        self._fetcher = fetcher

    def read_url(self, url: str) -> tuple[dict[str, Any], bool]:
        """读 URL。返回 (结果, 是否截断)。记录最终 URL/状态/类型/截断情况。"""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"不支持的协议：{parsed.scheme or '(无)'}")
        host = parsed.hostname or ""
        check_host(host, allow_private=self._allow_private)  # 越界抛 NetworkBlocked

        if self._fetcher is None:
            raise ToolError("联网抓取未配置（需要 HTTP 客户端）")
        status, content_type, body = self._fetcher(url)
        truncated = len(body) > self._max_bytes
        text = body[: self._max_bytes].decode("utf-8", errors="replace")
        return (
            {
                "url": url,
                "status": status,
                "content_type": content_type,
                "bytes": len(body),
                "text": text,
            },
            truncated,
        )

    def web_search(self, query: str) -> dict[str, Any]:
        """网页搜索需要外部搜索服务凭据。

        **未配置时如实返回结构化错误**，不伪造结果——设计计划要求不假装有能力。
        """
        raise ToolError("网页搜索未配置：需要在站点设置里配置搜索服务凭据")


# ---------- 网关 ----------


class ToolGateway:
    """工具调用的唯一入口。校验 → 权限 → 执行 → 限流 → 审计 → 结构化结果。"""

    def __init__(
        self,
        permissions: PermissionService,
        workspace_root: Path,
        *,
        network: NetworkTools | None = None,
        documents: FileService | None = None,
        terminal: TerminalTools | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._permissions = permissions
        self._files = FileTools(workspace_root, documents)
        self._documents = documents
        self._network = network
        self._terminal = terminal
        self._max_output_bytes = max_output_bytes
        self.memory_service: MemoryService | None = None
        self.memory_context: Callable[[], MemoryRunContext | None] = lambda: None

    @property
    def workspace_root(self) -> Path:
        """文件工具的根目录（解析后的真实路径，与请求范围里的路径同一种形式）。"""
        return self._files.resolve_path(".")

    def prepare_request(self, tool_name: str, params: dict[str, Any]) -> ToolRequest:
        """校验 + 规范化参数后构造权限请求。:meth:`invoke` 与扩展闸门共用这一段——
        闸门判定的范围与执行时检查的范围必须是同一个（解析后的绝对路径），
        会话授权才对得上。参数或路径非法时抛 ``ToolError`` / ``PathEscape``。
        """
        validate_params(tool_name, params)
        return self.build_request(tool_name, normalize_params(tool_name, params))

    def build_request(self, tool_name: str, params: dict[str, Any]) -> ToolRequest:
        """把工具调用转成权限判定用的请求（含资源范围）。"""
        capability = capability_of(tool_name)
        paths: tuple[str, ...] = ()
        domains: tuple[str, ...] = ()
        command: str | None = None
        if capability in (Capability.FILE_READ, Capability.FILE_WRITE):
            resolved = self._files.resolve_path(params.get("path", "."))
            paths = (str(resolved),)
        elif capability is Capability.NETWORK:
            from urllib.parse import urlparse

            if tool_name == "read_url":
                domains = (urlparse(params.get("url", "")).hostname or "",)
            else:
                domains = ()  # 搜索的目标由服务端决定
        elif capability is Capability.TERMINAL:
            # 命令文本必须进请求：破坏性命令识别（§11.2）就看它
            command = str(params.get("command", ""))
        return ToolRequest(
            tool_name=tool_name,
            capability=capability,
            scope=ResourceScope(paths=paths, domains=domains, command=command),
            raw_params=params,
        )

    def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        conversation_id: str,
        *,
        mode: ExecutionMode = ExecutionMode.BUILTIN_TOOLS,
        user_confirmed: bool = False,
    ) -> ToolResult:
        """执行一次工具调用。任何失败都返回结构化 ``ToolResult``（不抛给调用方）。"""
        # 1. 参数模式校验（含规范化），并解析出资源范围
        try:
            request = self.prepare_request(tool_name, params)
        except (PathEscape, ToolError) as exc:
            return ToolResult(ok=False, error=str(exc))
        params = request.raw_params  # 规范化后的参数：空的可选参数已回落默认

        if tool_name == "add_session_memory":
            context = self.memory_context()
            if (
                self.memory_service is None
                or context is None
                or context.conversation_id != conversation_id
                or context.run_id != params["run_id"]
            ):
                return ToolResult(ok=False, error="记忆工具运行上下文已失效")
            try:
                item = self.memory_service.add_from_model(
                    context,
                    params["call_id"],
                    params["content"],
                    lambda c: self.memory_context() == c,
                )
                return ToolResult(
                    ok=True,
                    data={
                        "id": item.id,
                        "saved": True,
                        "message": "已保存到当前分支会话记忆，下一轮更新记忆提醒。",
                    },
                )
            except Exception as exc:
                return ToolResult(ok=False, error=f"记忆写入失败：{exc}")

        # 2. 权限与资源范围检查（含高影响判定）
        decision = self._permissions.authorize(
            request, conversation_id, mode=mode, user_confirmed=user_confirmed
        )
        if decision.decision is Decision.DENY:
            return ToolResult(ok=False, error=f"权限网关拒绝：{decision.reason}")
        if decision.decision is Decision.CONFIRM and not user_confirmed:
            return ToolResult(
                ok=False,
                error=f"需要用户确认：{decision.reason}",
                data={"needs_confirmation": True, "rule": decision.matched_rule},
            )

        # 3. 执行（含 4. 输出限制）
        try:
            return self._execute(tool_name, params)
        except (ToolError, PathEscape, NetworkBlocked) as exc:
            return ToolResult(ok=False, error=str(exc))
        except Exception as exc:  # 工具实现不该把异常抛给模型
            return ToolResult(ok=False, error=f"工具执行失败：{exc}")

    def _execute(self, tool_name: str, params: dict[str, Any]) -> ToolResult:
        if tool_name == "read_document":
            return self._read_document(params)
        handler = getattr(self._files, tool_name, None)
        if handler is not None and tool_name != "read_url":
            result = handler(**params)
            # search_text 返回 (结果, 截断标记)
            if isinstance(result, tuple):
                data, truncated = result
                return ToolResult(ok=True, data=data, truncated=truncated)
            return ToolResult(ok=True, data=result)

        if tool_name == "read_url":
            if self._network is None:
                raise ToolError("联网工具未配置")
            data, truncated = self._network.read_url(**params)
            return self._apply_output_limit(data, truncated)
        if tool_name == "web_search":
            if self._network is None:
                raise ToolError("联网工具未配置")
            return ToolResult(ok=True, data=self._network.web_search(**params))
        if tool_name == "run_command":
            if self._terminal is None:
                raise ToolError("终端执行器未配置（直接终端模式未启用）")
            data = self._terminal.run_command(**params)
            # 成败看退出码（§10.5）；失败也把结果交给模型看，不吞输出
            notes = tuple(str(n) for n in data.get("notes") or ())
            return ToolResult(
                ok=True,
                data=data,
                truncated=bool(data.get("truncated")),
                notes=notes,
            )
        raise InvalidParams(f"未知工具：{tool_name}")

    def _read_document(self, params: dict[str, Any]) -> ToolResult:
        """精确读取附件文档（设计计划 §八.2，透传 :meth:`FileService.read`）。

        可读集合是**用户主动登记的附件文档**（磁盘 TXT/MD 或剪贴板文档），
        不在路径守卫的工作区内——这里的边界是登记表成员资格，不是路径。
        资源范围沿用文件读取的默认解析（工作区根），与其他读取工具同档授权。
        """
        if self._documents is None:
            raise ToolError("文档服务未配置")
        try:
            result = self._documents.read(
                params["file_id"],
                params.get("start_line", 1),
                params.get("end_line"),
            )
        except RangeLimitExceeded as exc:
            # 超上限拒绝调用（不擅自截取），结构化给出申请范围/上限/建议分段
            return ToolResult(
                ok=False,
                error=str(exc),
                data={
                    "requested_range": list(exc.requested),
                    "limit": exc.limit,
                    "total_lines": exc.total_lines,
                    "suggested_segments": [list(s) for s in exc.suggested_segments],
                },
            )
        except FileReadError as exc:
            return ToolResult(ok=False, error=str(exc))
        data = {
            "file_id": result.file_id,
            "path": result.path,
            "requested_range": list(result.requested_range),
            "actual_range": list(result.actual_range),
            "total_lines": result.total_lines,
            "content_hash": result.content_hash,
            "encoding": result.encoding,
            "truncated_lines": list(result.truncated_lines),
            "changed_since_index": result.changed_since_index,
            "text": result.text,
        }
        notes: list[str] = []
        if result.changed_since_index:
            notes.append("文档自登记以来已变化，行号按当前版本计算")
        if result.truncated_lines:
            notes.append("超长行已截断：" + "、".join(str(n) for n in result.truncated_lines))
        limited = self._apply_output_limit(data, truncated=False)
        return ToolResult(
            ok=True,
            data=limited.data,
            truncated=limited.truncated,
            notes=(*notes, *limited.notes),
        )

    def _apply_output_limit(self, data: dict[str, Any], truncated: bool) -> ToolResult:
        """输出大小上限（§9.3：超过时要求模型分段或缩小目标）。"""
        text = data.get("text")
        if isinstance(text, str):
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_output_bytes:
                data = {
                    **data,
                    "text": encoded[: self._max_output_bytes].decode("utf-8", errors="replace"),
                }
                truncated = True
        notes = ("输出超过上限已截断，请分段读取或缩小目标",) if truncated else ()
        return ToolResult(ok=True, data=data, truncated=truncated, notes=notes)
