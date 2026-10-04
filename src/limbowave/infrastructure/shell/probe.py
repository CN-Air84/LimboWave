"""Shell 环境探测（Task 7.1 / 设计计划 §10.1）。

pwsh 优先，缺失回退 Windows PowerShell 5.1 —— 回退时 ``is_fallback=True``，
界面**必须明示**（设计计划要求：不能让用户以为还是 PowerShell 7）。

探测结果给模型用（:meth:`ShellEnvironment.describe`）：shell 名称与版本、
系统版本、工作目录、默认编码、路径语法、以及「这不是 Bash」的明确提醒。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

from limbowave.domain.shell import ShellEnvironment, ShellKind

_PROBE_TIMEOUT = 20


def find_powershell() -> tuple[str, ShellKind] | None:
    """按 pwsh → powershell 顺序找可执行文件。都找不到返回 None。"""
    for name, kind in (("pwsh", ShellKind.PWSH), ("powershell", ShellKind.WINDOWS_POWERSHELL)):
        executable = shutil.which(name)
        if executable:
            return executable, kind
    return None


def probe_shell(
    working_directory: Path | None = None, *, host_platform: str | None = None
) -> ShellEnvironment | None:
    """探测 shell 环境。找不到任何 shell 返回 None（调用方应禁用终端功能）。"""
    if (host_platform or sys.platform) != "win32":
        return _probe_posix(working_directory)
    found = find_powershell()
    if found is None:
        return None
    executable, kind = found
    version = _query_version(executable)
    return ShellEnvironment(
        kind=kind,
        executable=executable,
        version=version,
        os_version=f"{platform.system()} {platform.release()} ({platform.version()})",
        working_directory=str(working_directory or Path.cwd()),
        default_encoding="UTF-8（执行层强制 Input/Output/$OutputEncoding）",
        path_syntax="Windows 路径（C:\\dir\\file）；分隔符 \\，PowerShell 也接受 /",
        is_fallback=kind is ShellKind.WINDOWS_POWERSHELL,
    )


def _query_version(executable: str) -> str:
    """问 shell 自己的版本号。失败返回 "unknown"（不阻塞探测）。"""
    try:
        probe_command = "$PSVersionTable.PSVersion.ToString()"
        completed = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", probe_command],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (completed.stdout or "").strip()
    return text or "unknown"


def _probe_posix(working_directory: Path | None) -> ShellEnvironment | None:
    for name, kind in (("bash", ShellKind.BASH), ("sh", ShellKind.SH)):
        executable = shutil.which(name)
        if executable is None:
            continue
        version = "POSIX" if kind is ShellKind.SH else "unknown"
        if kind is ShellKind.BASH:
            try:
                result = subprocess.run(
                    [executable, "--noprofile", "--norc", "--version"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=_PROBE_TIMEOUT, check=False,
                )
                version = (result.stdout.splitlines() or ["unknown"])[0]
            except (OSError, subprocess.SubprocessError):
                pass
        return ShellEnvironment(
            kind=kind, executable=executable, version=version,
            os_version=f"{platform.system()} {platform.release()}",
            working_directory=str(working_directory or Path.cwd()),
            default_encoding="UTF-8（脚本与输出解码）",
            path_syntax="POSIX 路径（/home/user/file），区分大小写；分隔符 /",
        )
    return None
