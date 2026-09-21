"""Pi RPC 模式的极简 Python 客户端（Phase 0 验证用）。

严格遵循合同文档 §三 的传输约定：

- **只在字节层按 ``b"\\n"`` 分帧**，容忍行尾 ``\\r``。
  绝不使用 ``str.splitlines()`` —— 它会在 ``U+2028`` / ``U+2029`` 处切分，
  而这两个字符在 JSON 字符串内合法，会造成误切。
- ``stdout`` 只承载协议（响应与事件）；``stderr`` 是日志与诊断。
- 命令可带 ``id``，对应响应回显该 ``id``；**事件没有 id**，只能靠顺序与
  ``turn_start`` / ``turn_end`` 界定边界。

本文件是验证工具，不是最终实现。生产实现将是 ``AgentKernel`` 的
``PiKernelAdapter``（见 docs/architecture/adr-0001-agent-kernel.md）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

RESPONSE_TYPE = "response"


def resolve_pi_command() -> list[str]:
    """解析出启动 Pi 的 argv。

    刻意绕过 npm 的 ``pi.cmd`` 垫片：Windows 的 ``CreateProcess`` 不会用 PATHEXT
    补全，也要求批处理经命令解释器启动，直接定位 ``cli.js`` 更确定，也顺带暴露了
    最终适配器真正该依赖的入口路径。

    同理不调用 ``npm root -g`` —— ``npm`` 本身也是 ``.cmd``，同样会踩这个坑。
    改为从垫片位置反推全局 ``node_modules``。
    """
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("未找到 node，请先安装 Node.js（Pi 要求 >= 22.19.0）")

    relative = Path("@earendil-works") / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    candidates: list[Path] = []

    # 从 pi 垫片反推：<prefix>/npm/pi.cmd → <prefix>/npm/node_modules/...
    for shim_name in ("pi", "pi.cmd", "pi.ps1"):
        shim = shutil.which(shim_name)
        if shim:
            candidates.append(Path(shim).parent / "node_modules" / relative)

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / relative)
    candidates.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / relative)
    candidates.append(Path("/usr/local/lib/node_modules") / relative)
    candidates.append(Path("/usr/lib/node_modules") / relative)

    for candidate in candidates:
        if candidate.is_file():
            return [node, str(candidate)]

    searched = "\n".join(f"  {path}" for path in candidates)
    raise RuntimeError(
        "未找到 Pi CLI 入口 cli.js。已查找：\n"
        f"{searched}\n请先执行：npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
    )


class PiRpcClient:
    """最小可用的 Pi RPC 客户端：同步请求/响应 + 事件收集。"""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        stderr_to: Path | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = str(cwd) if cwd is not None else None
        self._env = env
        self._stderr_to = stderr_to

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._event_cv = threading.Condition(self._lock)
        self._events: list[dict[str, Any]] = []
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._stderr_lines: list[str] = []
        self._counter = 0
        self._closed = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv,
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def close(self, timeout: float = 10.0) -> None:
        if self._proc is None:
            return
        self._closed = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()  # stdin EOF → 优雅退出，退出码 0
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=timeout)

    def kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc else None

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr_lines)

    # ---------- 读取线程 ----------

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buf = b""
        while True:
            chunk = self._proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # 只在 b"\n" 上分帧；\r\n 靠 strip 尾部 \r 兼容
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._dispatch(line.rstrip(b"\r"))
        if buf.strip():
            self._dispatch(buf.rstrip(b"\r"))

    def _dispatch(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            with self._lock:
                self._events.append({"_unparsed": raw.decode("utf-8", "replace")})
                self._event_cv.notify_all()
            return

        if message.get("type") == RESPONSE_TYPE and message.get("id") in self._pending:
            done, box = self._pending.pop(message["id"])
            box["response"] = message
            done.set()
            return

        with self._lock:
            self._events.append(message)
            self._event_cv.notify_all()

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            text = raw.decode("utf-8", "replace")
            self._stderr_lines.append(text)
        if self._stderr_to is not None:
            self._stderr_to.write_text(self.stderr_text, encoding="utf-8")

    # ---------- 命令 ----------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def send(self, command: dict[str, Any]) -> str:
        assert self._proc is not None and self._proc.stdin is not None
        cmd = dict(command)
        cmd.setdefault("id", self._next_id(str(cmd["type"])))
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        # 写入也必须走字节，且不需转义 U+2028/2029 —— JSON 允许它们在字符串内
        self._proc.stdin.write(line.encode("utf-8"))
        self._proc.stdin.flush()
        return cmd["id"]

    def request(self, command: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        assert self._proc is not None, "先调用 start()"
        cmd = dict(command)
        cmd.setdefault("id", self._next_id(str(cmd["type"])))
        done = threading.Event()
        box: dict[str, Any] = {}
        with self._lock:
            self._pending[cmd["id"]] = (done, box)
        self.send(cmd)
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(cmd["id"], None)
            raise TimeoutError(f"命令 {cmd['type']} 在 {timeout}s 内无响应")
        return box["response"]

    # ---------- 事件 ----------

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            out = self._events[:]
            self._events.clear()
            return out

    def wait_for_event(self, event_type: str, timeout: float = 60.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._event_cv:
            while True:
                for index, event in enumerate(self._events):
                    if event.get("type") == event_type:
                        return self._events.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._event_cv.wait(remaining)

    def wait_for_agent_settled(self, timeout: float = 120.0) -> bool:
        """等待 ``agent_settled``：无自动重试、无压缩重试、无排队续跑。"""
        return self.wait_for_event("agent_settled", timeout) is not None


def default_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PI_SKIP_VERSION_CHECK"] = "1"  # 关闭启动期版本检查出网
    env["PI_OFFLINE"] = "1"  # 关闭全部启动期网络操作（遥测/包检查）
    if extra:
        env.update(extra)
    return env


def main() -> int:
    """自检：能否启动 Pi 并拿到 get_state。"""
    argv = [*resolve_pi_command(), "--mode", "rpc", "--no-session"]
    client = PiRpcClient(argv, env=default_env())
    client.start()
    try:
        response = client.request({"type": "get_state"}, timeout=30)
        print(json.dumps(response, ensure_ascii=False, indent=2))
    finally:
        client.close()
    print(f"退出码={client.returncode}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
