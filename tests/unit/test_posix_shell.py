"""Platform-independent probing tests and native POSIX execution tests."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from limbowave.application.services.tool_gateway import TerminalTools
from limbowave.domain.shell import ShellEnvironment, ShellKind, ShellResult
from limbowave.infrastructure.shell import create_executor
from limbowave.infrastructure.shell import probe as probe_module


def test_posix_probe_falls_back_to_sh(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        probe_module.shutil, "which", lambda name: "/bin/sh" if name == "sh" else None,
    )
    env = probe_module.probe_shell(tmp_path, host_platform="linux")
    assert env is not None and env.kind is ShellKind.SH
    assert "POSIX" in env.path_syntax
    assert "不是 Bash" not in env.describe()
    assert "POSIX 语法" in env.describe()


def test_missing_posix_shell_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda name: None)
    assert probe_module.probe_shell(host_platform="linux") is None


def test_unknown_exit_is_not_success() -> None:
    result = ShellResult("bash", "id", "/tmp", None, False, "", "cannot start")
    assert not result.succeeded


def test_posix_commands_do_not_get_powershell_diagnostics() -> None:
    class Runner:
        def run(self, command: str, *, working_directory: Path | None = None) -> ShellResult:
            return ShellResult("bash", "id", "/tmp", 0, False, "", "")
    result = TerminalTools(Runner()).run_command("export X=hello; cat /dev/null")
    assert "bash_syntax_diagnostics" not in result


@pytest.fixture
def posix_env(tmp_path: Path) -> ShellEnvironment:
    if os.name == "nt":
        pytest.skip("requires a native POSIX kernel, not a mocked Windows platform")
    env = probe_module.probe_shell(tmp_path)
    assert env is not None, "Linux test runner must have bash or sh"
    return env


def test_native_posix_unicode_cwd_and_exit(posix_env: ShellEnvironment, tmp_path: Path) -> None:
    directory = tmp_path / "中文 space's"
    directory.mkdir()
    result = create_executor(posix_env).run(
        "printf '你好\n'; pwd; printf 'warning\n' >&2", working_directory=directory,
    )
    assert result.succeeded
    assert "你好" in result.stdout and str(directory) in result.stdout
    assert result.stderr == "warning\n"
    assert not create_executor(posix_env).run("exit 7").succeeded


def test_native_posix_timeout_and_descendant_cleanup(
    posix_env: ShellEnvironment, tmp_path: Path,
) -> None:
    from limbowave.infrastructure.shell.posix_executor import PosixShellExecutor
    marker = tmp_path / "child-finished"
    command = f"(sleep 2; touch {shlex.quote(str(marker))}) & wait"
    result = PosixShellExecutor(posix_env, timeout_seconds=1).run(command)
    assert result.timed_out and not result.succeeded
    import time
    time.sleep(1.5)
    assert not marker.exists(), "timed-out descendants must not continue running"


def test_native_posix_output_limit(posix_env: ShellEnvironment) -> None:
    from limbowave.infrastructure.shell.posix_executor import PosixShellExecutor
    result = PosixShellExecutor(posix_env, max_output_bytes=8).run("printf '123456789abcdef'")
    assert result.succeeded and result.truncated and result.stdout == "12345678"


def test_native_posix_ignores_startup_injection(
    posix_env: ShellEnvironment, tmp_path: Path, monkeypatch,
) -> None:
    startup = tmp_path / "startup.sh"
    startup.write_text("echo injected", encoding="utf-8")
    monkeypatch.setenv("BASH_ENV", str(startup))
    monkeypatch.setenv("ENV", str(startup))
    result = create_executor(posix_env).run("echo expected")
    assert result.stdout == "expected\n"
