"""Shell 执行的结果模型与环境信息（Phase 7 / 设计计划 §十）。

**退出码语义（§10.5，容易做错的地方）**：

- 不得仅凭 ``stderr`` 非空判定命令失败——PowerShell 会把警告、进度、
  以及**非终止错误**都写 stderr，而命令可能整体成功；
- 也不得仅凭退出码 0 忽略 PowerShell 非终止错误——非终止错误不设置退出码，
  但确实表明有步骤失败了。

因此结果里**同时**保留：退出码、stdout、stderr、以及单独采集的
``powershell_errors``（来自 ``$Error``）。判定「成功」是这三者的组合，
见 :meth:`ShellResult.succeeded`。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ShellKind(enum.StrEnum):
    """Windows: pwsh / PowerShell；POSIX: Bash / sh。"""

    BASH = "bash"
    SH = "sh"
    PWSH = "pwsh"  # PowerShell 7+
    WINDOWS_POWERSHELL = "powershell"  # Windows PowerShell 5.1


@dataclass(frozen=True, slots=True)
class ShellEnvironment:
    """会话级 shell 环境信息（§10.1：每次会话向模型提供）。

    目的是让模型**不假设 Bash 语法**——它需要知道自己在什么 shell 里。
    """

    kind: ShellKind
    executable: str
    version: str
    os_version: str
    working_directory: str
    default_encoding: str
    path_syntax: str
    is_fallback: bool = False  # 是否回退到了 Windows PowerShell（界面需明示）

    def describe(self) -> str:
        """给模型看的环境信息块，按实际 Shell 描述语法与路径。"""
        if self.kind in (ShellKind.BASH, ShellKind.SH):
            return (
                f"## 执行环境\n- Shell：{self.kind.value} {self.version}\n"
                f"- 系统：{self.os_version}\n- 当前工作目录：{self.working_directory}\n"
                f"- 默认编码：{self.default_encoding}\n- 路径语法：{self.path_syntax}\n"
                "- 使用当前 Shell 的语法，不要使用 PowerShell 命令。"
                + ("sh 模式仅保证 POSIX 语法，不要使用 Bash 专属语法。"
                   if self.kind is ShellKind.SH else "")
            )
        shell_note = (
            "（PowerShell 7 不可用，已回退 Windows PowerShell 5.1——"
            "部分 7.0+ 语法不可用，如 && 串联、三元运算符）"
            if self.is_fallback
            else ""
        )
        return (
            f"## 执行环境\n"
            f"- Shell：{self.kind.value} {self.version}{shell_note}\n"
            f"- 系统：{self.os_version}\n"
            f"- 当前工作目录：{self.working_directory}\n"
            f"- 默认编码：{self.default_encoding}\n"
            f"- 路径语法：{self.path_syntax}\n"
            f"- **这不是 Bash**：不要使用 export、`VAR=value cmd`、source、/dev/null、"
            f"heredoc 等你熟悉的 Bash 写法；用 $env:NAME 设环境变量、用 ; 串联命令。"
        )


@dataclass(frozen=True, slots=True)
class ShellResult:
    """一次 shell 执行的结构化结果（§10.5 的返回结构）。"""

    shell: str
    command_id: str
    working_directory: str
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    powershell_errors: tuple[str, ...] = ()
    duration_ms: int = 0
    truncated: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        """成败按**退出码与超时**判定（§10.5）。

        - **不看 stderr 是否为空**：警告、进度条也走 stderr，命令可能整体成功；
        - **不把 ``$Error`` 非空等同于失败**：PowerShell 的非终止错误不设退出码，
          它表明「有步骤出问题」，但整条命令可能仍然完成了主体工作。
          这类信息**如实上报**（见 :meth:`has_powershell_errors` 与 notes），
          由模型判断，而不是我们替它判死。
        """
        if self.timed_out:
            return False
        return self.exit_code == 0

    @property
    def has_powershell_errors(self) -> bool:
        """退出码为 0 但记录了 PowerShell 非终止错误——**不得忽略**（§10.5）。"""
        return self.succeeded and bool(self.powershell_errors)

    def to_payload(self) -> dict[str, object]:
        """转成给模型的结构化载荷。"""
        notes = list(self.notes)
        if self.has_powershell_errors:
            notes.append(
                f"退出码为 0，但记录了 {len(self.powershell_errors)} 条 PowerShell "
                "非终止错误——命令可能只完成了部分步骤，请检查 powershell_errors"
            )
        return {
            "shell": self.shell,
            "command_id": self.command_id,
            "working_directory": self.working_directory,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "powershell_errors": list(self.powershell_errors),
            "succeeded": self.succeeded,
            "truncated": self.truncated,
            "notes": notes,
        }
