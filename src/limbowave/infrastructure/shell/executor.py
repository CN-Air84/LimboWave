"""PowerShell 执行器：临时脚本 + 编码统一 + 结构化返回（Phase 7 / 设计计划 §十）。

为什么不用 ``-Command`` 单行（§10.3）：命令行长度有上限，引号与转义要过
Node/PowerShell 两层序列化，长命令必然出问题。这里走**受控临时 .ps1**：

1. 临时目录写 ``script.ps1``。**编码有个实测出来的坑**：设计计划 §10.3 要求
   「UTF-8 无 BOM」，但 Windows PowerShell 5.1 在**没有 BOM** 时按系统 ANSI
   （中文机器上是 GBK）解析脚本——脚本里一出现中文就会变成语法错误
   （实测：`Write-Output "你好"` 报 TerminatorExpectedAtEndOfString）。
   因此这里的规则是：**脚本含非 ASCII 字符时写 BOM**（5.1 必需、7.0+ 无害），
   纯 ASCII 脚本保持无 BOM（计划的原意）。这是对计划的一处**有意偏离**，
   依据是实测行为而不是偏好；
2. 脚本内统一编码（§10.2）：``InputEncoding`` / ``OutputEncoding`` / ``$OutputEncoding``
   全部设为 ``UTF8Encoding($false)``（无 BOM 语义）；
3. 显式 ``Set-Location`` 到目标工作目录；
4. 注入错误处理：``$ErrorActionPreference = 'Continue'`` + ``$Error.Clear()``——
   让非终止错误被记录而不是中断脚本；
5. 脚本末尾把 ``$LASTEXITCODE`` 与 ``$Error`` 写进**独立的 result.json**——
   元数据与命令输出分开，stdout 保持纯净；
6. 分别捕获 stdout / stderr / 退出码 / 超时；
7. 无论成败都清理临时文件。

超时用 ``taskkill /F /T`` 杀**进程树**（§十 契约提示：不走 PATH，避免被劫持）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from limbowave.domain.shell import ShellEnvironment, ShellResult

# 输出上限：超限截断并如实标注（不静默丢内容）
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 120

# 脚本末尾写元数据的标记文件（与 stdout 分离，避免污染命令输出）
_RESULT_FILE = "result.json"

# 严格模式前缀：编码 + 工作目录 + 错误收集
# 注意 UTF8Encoding($false) —— 无 BOM 语义；Encoding.UTF8 会带 BOM 语义
_PREAMBLE = """$ErrorActionPreference = 'Continue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location -LiteralPath __CWD__
$Error.Clear()
"""

# 收尾：把退出码与错误列表写进 result.json
_EPILOGUE = """
$__lw_exit = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
$__lw_errors = @($Error | ForEach-Object { $_.ToString() })
$__lw_payload = @{ exit_code = $__lw_exit; errors = $__lw_errors } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText(__RESULT__, $__lw_payload, [System.Text.UTF8Encoding]::new($false))
"""


class PowerShellExecutor:
    """在受控临时脚本里执行 PowerShell 命令。"""

    def __init__(
        self,
        environment: ShellEnvironment,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._env = environment
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes

    def run(self, command: str, *, working_directory: Path | None = None) -> ShellResult:
        """执行一段 PowerShell 代码。任何失败都返回结构化结果，不抛。"""
        command_id = f"cmd_{uuid4().hex[:12]}"
        cwd = Path(working_directory or self._env.working_directory)
        workdir = Path(tempfile.mkdtemp(prefix="limbowave-sh-"))
        script_path = workdir / "script.ps1"
        result_path = workdir / _RESULT_FILE
        started = time.monotonic()
        try:
            script_path.write_bytes(_encode_script(self._build_script(command, cwd, result_path)))
            return self._execute(command_id, argv_of(script_path), result_path, cwd, started)
        except OSError as exc:
            return ShellResult(
                shell=self._env.kind.value,
                command_id=command_id,
                working_directory=str(cwd),
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr=f"执行器内部错误：{exc}",
                notes=(f"临时脚本准备失败：{exc}",),
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)  # 无论成败都清理

    # ---------- 内部 ----------

    def _build_script(self, command: str, cwd: Path, result_path: Path) -> str:
        """组装脚本：前导（编码/目录/错误收集）+ 命令 + 收尾（元数据落盘）。"""
        # 显式占位符替换，不用 str.format——PowerShell 代码里全是花括号
        preamble = _PREAMBLE.replace("__CWD__", _ps_literal(str(cwd)))
        epilogue = _EPILOGUE.replace("__RESULT__", _ps_literal(str(result_path)))
        return f"{preamble}{command}\n{epilogue}"

    def _execute(
        self,
        command_id: str,
        argv: list[str],
        result_path: Path,
        cwd: Path,
        started: float,
    ) -> ShellResult:
        timed_out = False
        full_argv = [self._env.executable, *argv]
        try:
            # 用 Popen 而不是 run：超时要按 **PID** 杀进程树（按标题过滤杀不掉）
            process = subprocess.Popen(
                full_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            return ShellResult(
                shell=self._env.kind.value,
                command_id=command_id,
                working_directory=str(cwd),
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr=f"无法启动 shell：{exc}",
                notes=("shell 可执行文件不可用",),
            )

        try:
            stdout_raw, stderr_raw = process.communicate(timeout=self._timeout)
            process_exit: int | None = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(process.pid)
            try:
                stdout_raw, stderr_raw = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout_raw, stderr_raw = b"", b""
            process_exit = None

        # 退出码与 PowerShell 错误从标记文件读（与 stdout 分离）
        exit_code, ps_errors = self._read_metadata(result_path, process_exit)
        duration_ms = int((time.monotonic() - started) * 1000)

        stdout, out_truncated = self._decode(stdout_raw)
        stderr, err_truncated = self._decode(stderr_raw)
        notes: list[str] = []
        if timed_out:
            notes.append(f"命令在 {self._timeout} 秒内未完成，已终止进程树")
        if out_truncated or err_truncated:
            notes.append("输出超过上限已截断")

        return ShellResult(
            shell=self._env.kind.value,
            command_id=command_id,
            working_directory=str(cwd),
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            powershell_errors=ps_errors,
            duration_ms=duration_ms,
            truncated=out_truncated or err_truncated,
            notes=tuple(notes),
        )

    def _read_metadata(
        self, result_path: Path, process_exit: int | None
    ) -> tuple[int | None, tuple[str, ...]]:
        """读 result.json。文件缺失（脚本早期崩溃/被杀）时退回进程退出码。"""
        if not result_path.is_file():
            return process_exit, ()
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return process_exit, ()
        exit_code = payload.get("exit_code")
        errors = payload.get("errors") or []
        return (
            int(exit_code) if isinstance(exit_code, int) else process_exit,
            tuple(str(e) for e in errors if str(e).strip()),
        )

    def _decode(self, raw: bytes) -> tuple[str, bool]:
        """解码并归一化换行。超限截断（**先截断再解码**，避免截出半个字符）。"""
        truncated = len(raw) > self._max_output
        if truncated:
            raw = raw[: self._max_output]
        text = raw.decode("utf-8", errors="replace")
        # CRLF / CR 归一为 LF（§10.2 的换行归一化）；去掉可能的前导 BOM
        text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
        return text, truncated

    @staticmethod
    def _kill_tree(pid: int) -> None:
        """按 PID 杀进程树（命令可能已经 fork 出子进程）。

        Windows 用 System32 下的 taskkill 绝对路径——**不走 PATH**，
        避免被同名程序劫持（§十 契约提示）。
        """
        if os.name == "nt":
            taskkill = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            )
            if taskkill.is_file():
                with suppress_all():
                    subprocess.run(
                        [str(taskkill), "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                return
        if sys.platform != "win32":
            # 所有 POSIX 执行器都先创建独立 session，不能杀应用所在进程组。
            with suppress_all():
                os.killpg(pid, signal.SIGKILL)
            return
        # 找不到 taskkill：尽力终止直接子进程
        with suppress_all():
            os.kill(pid, 9)


# UTF-8 BOM：PowerShell 5.1 靠它识别 UTF-8 脚本（否则按 ANSI 解析）
_UTF8_BOM = b"\xef\xbb\xbf"


def _encode_script(body: str) -> bytes:
    """编码脚本字节。含非 ASCII 时加 BOM——这是 5.1 的硬要求（见模块文档）。"""
    encoded = body.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    if any(byte > 0x7F for byte in encoded):
        return _UTF8_BOM + encoded
    return encoded


def argv_of(script_path: Path) -> list[str]:
    """构造 shell 调用参数。``-File`` 走脚本文件而不是 ``-Command`` 单行（§10.3）。"""
    return [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]


def _ps_literal(value: str) -> str:
    """把字符串变成 PowerShell 单引号字面量（单引号内只需把 ' 翻倍）。"""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


class suppress_all:
    """吞掉清理阶段的任何异常——清理失败不该影响结果返回。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True
