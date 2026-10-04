"""PowerShell 命令的 Bash 误用检测（设计计划 §十.4）。

环境是 Windows PowerShell（pwsh 优先，回退 Windows PowerShell 5.1），
但模型常按 Bash 习惯写命令。检测到疑似误用时：

- **不自动改写并立即执行**（那会在用户不知情下跑一条不同的命令）；
- 返回**结构化诊断**；
- 给模型一次生成 PowerShell 修正版的机会；
- 修正版照常走权限检查。

本模块只做静态检测，不执行任何命令。纯函数，可测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShellDiagnostic:
    """一条误用诊断。``pattern`` 供测试与 UI 引用，``suggestion`` 给模型看。"""

    pattern: str
    detail: str
    suggestion: str


# 检测规则：(模式名, 正则, 说明, 建议)
_RULES: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    (
        "export",
        re.compile(r"^\s*export\s+\w+=", re.MULTILINE),
        "Bash 的 export 在 PowerShell 中无效",
        '用 $env:NAME = "value" 设置环境变量',
    ),
    (
        "inline_assign",
        re.compile(r"^\s*[A-Za-z_]\w*=\S+\s+\S+", re.MULTILINE),
        "Bash 的 VAR=value command 前缀赋值在 PowerShell 中无效",
        '先赋值 $VAR = "value"，再单独执行命令',
    ),
    (
        "source",
        re.compile(r"^\s*(source|\.)\s+\S+", re.MULTILINE),
        "Bash 的 source 在 PowerShell 中不同",
        "用 . .\\script.ps1（点号后跟路径）或在当前会话执行脚本内容",
    ),
    (
        "dev_null",
        re.compile(r"/dev/null"),
        "/dev/null 不存在于 Windows",
        "用 $null（PowerShell 的空值）或 Out-Null",
    ),
    (
        "heredoc",
        re.compile(r"<<-?\s*['\"]?\w+", re.MULTILINE),
        "Bash heredoc 语法在 PowerShell 中不存在",
        '用 here-string @" ... "@ 或 Set-Content',
    ),
    (
        "rm_rf",
        re.compile(r"\brm\s+-[rf]{1,2}\b"),
        "rm 在 PowerShell 是 Remove-Item 的别名，但不支持 -rf 组合写法",
        "用 Remove-Item -Recurse -Force（并注意这是破坏性操作）",
    ),
    (
        "cp_mv_flags",
        re.compile(r"\b(cp|mv)\s+-[a-zA-Z]*[rRvV]\b"),
        "cp/mv 在 PowerShell 是别名，参数语义不同",
        "用 Copy-Item / Move-Item 并显式写参数名",
    ),
    (
        "bash_and_chain",
        re.compile(r"&&\s*\S"),
        "PowerShell 5.1 不支持 && 串联（7.0+ 支持）",
        "用 ; 串联，或按版本判断；关键流程用分步执行",
    ),
    (
        "dollar_bang",
        re.compile(r"\$\!"),
        "$! 是 Bash 的上一条命令退出码，PowerShell 中不存在",
        "用 $LASTEXITCODE（外部命令）或 $?（成功布尔）",
    ),
    (
        "single_quote_var",
        re.compile(r"'[^']*\$[A-Za-z_]\w*[^']*'"),
        "PowerShell 单引号字符串不展开变量（Bash 部分场景会）",
        "需要展开时用双引号字符串",
    ),
)


def detect_bash_isms(command: str) -> list[ShellDiagnostic]:
    """检测命令中的 Bash 习惯用法。返回全部命中的诊断（可能多条）。纯函数。"""
    found: list[ShellDiagnostic] = []
    for name, pattern, detail, suggestion in _RULES:
        if pattern.search(command):
            found.append(ShellDiagnostic(pattern=name, detail=detail, suggestion=suggestion))
    return found


def has_bash_isms(command: str) -> bool:
    """是否疑似 Bash 语法。供网关快速分流。"""
    return bool(detect_bash_isms(command))
