"""Pi RPC 子进程客户端（生产实现）。

严格遵循 docs/architecture/pi-runtime-contract.md §三 的传输约定：

- **只在字节层按 ``b"\\n"`` 分帧**，容忍行尾 ``\\r``。绝不使用 ``str.splitlines()`` ——
  它会在 ``U+2028`` / ``U+2029`` 处切分，而这两个字符在 JSON 字符串内合法。
- ``stdout`` 只承载协议（响应与事件）；``stderr`` 是日志与诊断。
- 命令可带 ``id``，对应响应回显该 ``id``；**事件没有 id**，靠顺序与边界事件界定。
- 优雅关闭 = 关闭 stdin（EOF → 退出码 0）；不要依赖 SIGINT（Pi 无处理器）。

与 ``tools/pi-verify/rpc_client.py``（验证脚本）的区别：这里是纯 async 实现，
供 qasync 在 GUI 线程驱动；不引入 threading。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from limbowave.infrastructure.diagnostics.sanitize import safe_text

RESPONSE_TYPE = "response"
_LOG = logging.getLogger(__name__)
_STDERR_CACHE_CHARS = 256 * 1024
_STDERR_CACHE_LINES = 1024
_CREATE_NO_WINDOW = 0x08000000
_BACKGROUND_JSON_THRESHOLD = 64 * 1024


def _decode_frame(raw: bytes) -> dict[str, Any]:
    message = json.loads(raw.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("RPC frame is not an object")
    return message


def _subprocess_creation_kwargs(platform: str = sys.platform) -> dict[str, Any]:
    """返回平台专用的子进程启动参数。

    无控制台的 Windows GUI 程序启动控制台子系统进程时，系统默认会另建控制台窗口。
    Pi 全程通过管道通信，不需要该窗口。
    """
    if platform == "win32":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


@dataclass(slots=True)
class SpawnSpec:
    """启动内核子进程所需的全部信息。由上层（bootstrap）组装。"""

    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    stderr_handler: Callable[[str], None] | None = None


@dataclass(slots=True)
class _PendingRequest:
    """一条待响应的命令。"""

    future: asyncio.Future[dict[str, Any]]


class PiRpcProcess:
    """一个 Pi RPC 子进程：异步命令/响应 + 事件流。"""

    def __init__(self, spec: SpawnSpec) -> None:
        self._spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._pending: dict[str, _PendingRequest] = {}
        self._event_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self._stderr_lines: deque[str] = deque()
        self._stderr_chars = 0
        # 优雅关闭（stdin EOF / 主动 kill）与崩溃要区分开，否则关闭时的正常退出会被
        # 误报成 runtime 崩溃，把已经完成的一轮标成 interrupted。
        self._expected_exit = False

    # ---------- 生命周期 ----------

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    async def start(self) -> None:
        if self.is_running:
            return
        self._expected_exit = False
        self._stderr_lines.clear()
        self._stderr_chars = 0
        self._proc = await asyncio.create_subprocess_exec(
            *self._spec.argv,
            cwd=self._spec.cwd,
            env=self._spec.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_subprocess_creation_kwargs(),
        )
        _LOG.info("pi.started", extra={"pid": self.pid})
        self._reader_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="pi-rpc-stderr")

    async def shutdown(self, timeout: float = 10.0) -> int | None:
        """优雅关闭：关闭 stdin（EOF → 退出码 0），超时后强杀。"""
        if self._proc is None:
            return None
        self._expected_exit = True
        if self._proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError):
                self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except TimeoutError:
            _LOG.warning("pi.shutdown_timeout", extra={"pid": self.pid})
            self._proc.kill()
            await self._proc.wait()
        readers = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        _LOG.info("pi.stopped", extra={"pid": self.pid, "returncode": self._proc.returncode})
        return self._proc.returncode

    async def kill(self) -> None:
        self._expected_exit = True
        if self._proc is not None and self._proc.returncode is None:
            _LOG.warning("pi.terminated", extra={"pid": self.pid})
            self._proc.kill()
            await self._proc.wait()

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr_lines)

    # ---------- 读取 ----------

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buf = b""
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                # 只在 b"\n" 上分帧；\r\n 靠 strip 尾部 \r 兼容（合同 §三.1）
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    await self._dispatch_async(line.rstrip(b"\r"))
            if buf.strip():
                await self._dispatch_async(buf.rstrip(b"\r"))
        finally:
            # 进程退出：让所有挂起的请求以异常结束，避免永远等待
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectionError(f"Pi 进程退出（rc={self.returncode}），请求未完成")
                    )
            self._pending.clear()
            _LOG.log(logging.INFO if self._expected_exit else logging.ERROR, "pi.stdout_closed",
                     extra={"pid": self.pid, "returncode": self.returncode,
                            "expected": self._expected_exit})
            # 如实上报退出事实，并标注是否为预期退出（优雅关闭）。事件名用 Pi 的
            # 风格以便适配器直接映射；是否算"崩溃"由适配器按 expected 判定。
            self._emit(
                {
                    "type": "process_exit",
                    "returncode": self.returncode,
                    "expected": self._expected_exit,
                }
            )

    def _dispatch(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            message = _decode_frame(raw)
        except (ValueError, UnicodeDecodeError):
            self._dispatch_invalid(raw)
            return
        self._dispatch_message(message)

    async def _dispatch_async(self, raw: bytes) -> None:
        if not raw.strip():
            return
        if len(raw) < _BACKGROUND_JSON_THRESHOLD:
            self._dispatch(raw)
            return
        try:
            message = await asyncio.to_thread(_decode_frame, raw)
        except (ValueError, UnicodeDecodeError):
            self._dispatch_invalid(raw)
            return
        self._dispatch_message(message)

    def _dispatch_invalid(self, raw: bytes) -> None:
        _LOG.warning("pi.invalid_frame", extra={"size_bytes": len(raw)})
        self._emit({"_unparsed": raw.decode("utf-8", "replace")})

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        # 命令响应：带 id 且 type == response，回显给对应请求
        if message.get("type") == RESPONSE_TYPE and message.get("id") in self._pending:
            pending = self._pending.pop(message["id"])
            if not pending.future.done():
                pending.future.set_result(message)
            return

        # 其余一律视为事件（含 extension_ui_request 等）
        self._emit(message)

    def _emit(self, message: dict[str, Any]) -> None:
        for handler in list(self._event_handlers):
            # 事件处理器不得拖垮读取循环
            try:
                handler(message)
            except Exception:
                _LOG.exception("pi.event_handler_failed")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        # ``StreamReader.readline()`` 受 asyncio 默认约 64 KiB 的单行上限约束。
        # PolicyEnforcementExtension 会把最终 provider payload 作为一条 JSONL 写到
        # stderr；长会话很容易超过该上限。readline 此时会抛 ValueError 并永久停止
        # 排空管道，Pi 随后阻塞在 stderr.write()，表现为用户消息已进入会话、但真正
        # 的 provider 请求始终没有发出。
        #
        # 这里改为固定大小读取并在字节层自行按 ``b"\n"`` 分帧：既不受单行限制，
        # 又能持续排空 OS 管道。完整帧仍交给上层解析，传输快照不因此丢字段。
        buf = bytearray()
        while True:
            chunk = await self._proc.stderr.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                newline = buf.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(buf[: newline + 1])
                del buf[: newline + 1]
                self._handle_stderr_frame(raw)
        if buf:
            self._handle_stderr_frame(bytes(buf))

    def _handle_stderr_frame(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        cached = text[-_STDERR_CACHE_CHARS:]
        self._stderr_lines.append(cached)
        self._stderr_chars += len(cached)
        while (
            self._stderr_chars > _STDERR_CACHE_CHARS
            or len(self._stderr_lines) > _STDERR_CACHE_LINES
        ):
            self._stderr_chars -= len(self._stderr_lines.popleft())
        # 结构化观测含完整请求/响应，只走原观测通道，绝不再复制进明文调试日志。
        if not text.lstrip().startswith("[LIMBOWAVE]"):
            _LOG.warning("pi.stderr: %s", safe_text(text.rstrip()), extra={"pid": self.pid})
        if self._spec.stderr_handler is not None:
            try:
                self._spec.stderr_handler(text)
            except Exception:
                _LOG.exception("pi.stderr_handler_failed")

    # ---------- 命令 ----------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    async def send(self, command: dict[str, Any]) -> str:
        """发送一条命令，返回其 id。不等待响应。"""
        if not self.is_running or self._proc is None or self._proc.stdin is None:
            raise ConnectionError("Pi 进程未运行")
        cmd = dict(command)
        cmd.setdefault("id", self._next_id(str(cmd["type"])))
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        async with self._write_lock:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        _LOG.debug("pi.command_sent", extra={"command": cmd["type"], "request_id": str(cmd["id"])})
        return str(cmd["id"])

    async def request(self, command: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """发送命令并等待其响应。超时或失败抛错。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        cmd = dict(command)
        cmd.setdefault("id", self._next_id(str(cmd["type"])))
        self._pending[str(cmd["id"])] = _PendingRequest(future=future)
        try:
            await self.send(cmd)
            response = await asyncio.wait_for(future, timeout=timeout)
            _LOG.log(logging.DEBUG if response.get("success", True) else logging.WARNING,
                     "pi.command_response", extra={"command": cmd["type"], "request_id": cmd["id"],
                                                   "success": response.get("success")})
            return response
        except TimeoutError:
            _LOG.warning("pi.command_timeout", extra={
                "command": cmd["type"], "request_id": cmd["id"], "timeout_seconds": timeout,
            })
            raise TimeoutError(f"命令 {cmd['type']} 在 {timeout}s 内无响应") from None
        finally:
            self._pending.pop(str(cmd["id"]), None)

    # ---------- 事件订阅 ----------

    def on_event(self, handler: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """订阅原始协议事件。返回退订函数。"""
        self._event_handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._event_handlers:
                self._event_handlers.remove(handler)

        return _unsubscribe


def resolve_pi_argv(
    *,
    node: str | None = None,
    cli_path: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """组装启动 Pi 的 argv。

    直接定位 ``node`` + ``cli.js``，**不走 npm 的 ``.cmd`` 垫片**——Windows 的
    ``CreateProcess`` 不用 PATHEXT 补全，批处理又要求经命令解释器启动，
    直接调用 ``node <cli.js>`` 最确定（合同 §八 / tools README）。
    """
    node = node or shutil.which("node")
    if node is None:
        raise RuntimeError("未找到 node，请先安装 Node.js（Pi 要求 >= 22.19.0）")

    if cli_path is None:
        cli_path = _locate_cli(node)
    argv = [node, str(cli_path)]
    if extra_args:
        argv.extend(extra_args)
    return argv


def _locate_cli(node: str | None = None) -> Path:
    """从 npm 垫片反推全局 node_modules 中的 cli.js。"""
    relative = Path("@earendil-works") / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    candidates: list[Path] = []
    for shim in ("pi", "pi.cmd", "pi.ps1"):
        found = shutil.which(shim)
        if found:
            shim_path = Path(found)
            resolved = shim_path.resolve()
            if resolved.name == "cli.js" and "pi-coding-agent" in resolved.parts:
                candidates.append(resolved)
            for bindir in (shim_path.parent, resolved.parent):
                candidates.append(bindir / "node_modules" / relative)
                candidates.append(bindir.parent / "lib" / "node_modules" / relative)

    # nvm/fnm/asdf: npm global packages belong to the selected Node prefix.
    selected_node = node or shutil.which("node")
    if selected_node:
        for bindir in (Path(selected_node).parent, Path(selected_node).resolve().parent):
            candidates.append(bindir / "node_modules" / relative)
            candidates.append(bindir.parent / "lib" / "node_modules" / relative)

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / relative)
    candidates.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / relative)
    candidates.append(Path("/usr/local/lib/node_modules") / relative)
    candidates.append(Path("/usr/lib/node_modules") / relative)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = "\n".join(f"  {p}" for p in candidates)
    raise RuntimeError(
        "未找到 Pi CLI 入口 cli.js。已查找：\n"
        f"{searched}\n请先执行：npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
    )
