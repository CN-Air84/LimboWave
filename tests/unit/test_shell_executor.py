"""Shell 执行器的领域与编码测试（Phase 7 / 设计计划 §十）。

分两层：
- **纯逻辑**（不跑 shell）：成功判定语义、脚本编码规则（BOM）、PS 字面量转义、
  环境信息块内容、argv 构造；
- **真实执行**（本机有 Windows PowerShell）：中文输入输出、非终止错误、
  退出码、工作目录、超时杀进程树。这些标记为需要真实 shell，缺失则跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.domain.shell import ShellEnvironment, ShellKind, ShellResult
from limbowave.infrastructure.shell.executor import (
    _encode_script,
    _ps_literal,
    argv_of,
)
from limbowave.infrastructure.shell.probe import find_powershell, probe_shell

_HAVE_POWERSHELL = find_powershell() is not None
requires_shell = pytest.mark.skipif(not _HAVE_POWERSHELL, reason="本机没有 PowerShell")


def _result(**overrides) -> ShellResult:
    base = {
        "shell": "pwsh",
        "command_id": "cmd_1",
        "working_directory": "C:/ws",
        "exit_code": 0,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
    }
    base.update(overrides)
    return ShellResult(**base)  # type: ignore[arg-type]


# ---------- 成功判定语义（§10.5） ----------


def test_exit_zero_with_stderr_is_success() -> None:
    """stderr 非空不等于失败：警告也走 stderr。"""
    result = _result(stderr="WARNING: something", exit_code=0)
    assert result.succeeded is True


def test_nonzero_exit_is_failure() -> None:
    assert _result(exit_code=1).succeeded is False
    assert _result(exit_code=3).succeeded is False


def test_timeout_is_failure() -> None:
    assert _result(timed_out=True, exit_code=None).succeeded is False


def test_exit_zero_with_powershell_errors_not_ignored() -> None:
    """退出码 0 但有非终止错误：不判失败，但**必须上报**（不忽略）。"""
    result = _result(exit_code=0, powershell_errors=("Cannot find path",))
    assert result.succeeded is True  # 退出码说成功
    assert result.has_powershell_errors is True  # 但错误被如实标记
    payload = result.to_payload()
    assert payload["powershell_errors"] == ["Cannot find path"]
    assert any("非终止错误" in n for n in payload["notes"])  # type: ignore[union-attr]


def test_payload_shape_matches_design() -> None:
    """§10.5 的返回结构：shell / command_id / cwd / exit_code / timed_out / stdout / stderr。"""
    payload = _result(stdout="out", stderr="err").to_payload()
    for key in (
        "shell",
        "command_id",
        "working_directory",
        "exit_code",
        "timed_out",
        "stdout",
        "stderr",
        "powershell_errors",
    ):
        assert key in payload


# ---------- 脚本编码（实测出来的 5.1 行为） ----------


def test_ascii_script_has_no_bom() -> None:
    """纯 ASCII 脚本保持无 BOM（计划原意）。"""
    encoded = _encode_script("Write-Output 'hello'")
    assert not encoded.startswith(b"\xef\xbb\xbf")


def test_non_ascii_script_gets_bom() -> None:
    """含中文的脚本必须有 BOM——否则 PowerShell 5.1 按 ANSI 解析、中文变语法错误。"""
    encoded = _encode_script('Write-Output "你好"')
    assert encoded.startswith(b"\xef\xbb\xbf")
    assert "你好".encode() in encoded


def test_script_uses_crlf() -> None:
    encoded = _encode_script("line1\nline2")
    assert b"line1\r\nline2" in encoded


def test_ps_literal_escapes_single_quote() -> None:
    assert _ps_literal("C:\\it's\\here") == "'C:\\it''s\\here'"


def test_argv_uses_file_not_command() -> None:
    """§10.3：走 -File 脚本文件，不拼 -Command 单行。"""
    argv = argv_of(Path("C:/tmp/s.ps1"))
    assert "-File" in argv
    assert "-Command" not in argv
    assert "-NoProfile" in argv
    assert "-NonInteractive" in argv


# ---------- 环境信息（§10.1） ----------


def test_environment_describe_warns_not_bash() -> None:
    env = ShellEnvironment(
        kind=ShellKind.WINDOWS_POWERSHELL,
        executable="powershell",
        version="5.1",
        os_version="Windows 10",
        working_directory="C:/ws",
        default_encoding="UTF-8",
        path_syntax="Windows",
        is_fallback=True,
    )
    text = env.describe()
    assert "这不是 Bash" in text
    assert "export" in text  # 明确点出不要用的写法
    assert "回退" in text  # 回退必须明示


def test_environment_no_fallback_note_when_pwsh() -> None:
    env = ShellEnvironment(
        kind=ShellKind.PWSH,
        executable="pwsh",
        version="7.4",
        os_version="Windows 11",
        working_directory="C:/ws",
        default_encoding="UTF-8",
        path_syntax="Windows",
    )
    assert "回退" not in env.describe()


# ---------- 真实执行 ----------


@requires_shell
def test_real_chinese_round_trip() -> None:
    """中文命令 + 中文输出：编码链路端到端正确（5.1 上这条最容易挂）。"""
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    result = executor.run('Write-Output "你好，世界"')
    assert result.succeeded is True, result.stderr
    assert result.stdout.strip() == "你好，世界"


@requires_shell
def test_real_nonterminating_error_reported() -> None:
    """非终止错误：退出码 0，但错误被采集并上报。"""
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    result = executor.run(r"Get-Item C:\definitely\missing\file.txt; Write-Output done")
    assert result.stdout.strip() == "done"
    assert result.exit_code == 0
    assert result.powershell_errors  # 采集到了
    assert result.has_powershell_errors is True
    payload = result.to_payload()
    assert payload["succeeded"] is True  # 退出码说成功
    assert payload["powershell_errors"]  # 但错误没被吞


@requires_shell
def test_real_exit_code_propagates() -> None:
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    result = executor.run("exit 7")
    assert result.exit_code == 7
    assert result.succeeded is False


@requires_shell
def test_real_working_directory_honored(tmp_path: Path) -> None:
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    result = executor.run("(Get-Location).Path", working_directory=tmp_path)
    assert result.succeeded is True, result.stderr
    assert Path(result.stdout.strip()).name == tmp_path.name


@requires_shell
def test_real_timeout_kills_process_tree() -> None:
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=2)
    result = executor.run("Start-Sleep -Seconds 60")
    assert result.timed_out is True
    assert result.succeeded is False
    assert result.notes  # 如实说明被终止


@requires_shell
def test_real_child_process_output_decoded() -> None:
    """子进程（cmd）输出也要按 UTF-8 解出来（§10.2 子进程输出编码）。"""
    from limbowave.infrastructure.shell import PowerShellExecutor

    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    command = (
        'cmd /c "echo hello-from-cmd"'
        if executor._env.kind is ShellKind.WINDOWS_POWERSHELL
        else '& $PSHOME/pwsh -NoProfile -Command "echo hello-from-cmd"'
    )
    result = executor.run(command)
    assert result.succeeded is True, result.stderr
    assert "hello-from-cmd" in result.stdout


@requires_shell
def test_real_temp_script_cleaned_up() -> None:
    """执行后临时脚本目录必须清掉（不在磁盘留下命令内容）。"""
    import tempfile

    from limbowave.infrastructure.shell import PowerShellExecutor

    before = set(Path(tempfile.gettempdir()).glob("limbowave-sh-*"))
    executor = PowerShellExecutor(probe_shell(host_platform="win32"), timeout_seconds=30)
    executor.run("Write-Output hi")
    after = set(Path(tempfile.gettempdir()).glob("limbowave-sh-*"))
    assert after == before  # 没有残留
