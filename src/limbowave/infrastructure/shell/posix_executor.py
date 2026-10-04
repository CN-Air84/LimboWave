"""POSIX shell commands in a private process session, never in the GUI session."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from limbowave.domain.shell import ShellKind, ShellResult
from limbowave.infrastructure.shell.executor import PowerShellExecutor


class PosixShellExecutor(PowerShellExecutor):
    """Reuse result decoding/limits and process cleanup, not PowerShell syntax.

    Output is spooled to private temporary files to avoid unbounded RAM and pipe
    deadlocks when a command forks. Only the configured prefix is read back.
    Background descendants in this session are terminated on completion/timeout;
    this is lifecycle management, not a sandbox against deliberately detached jobs.
    """

    def run(self, command: str, *, working_directory: Path | None = None) -> ShellResult:
        command_id = f"cmd_{uuid4().hex[:12]}"
        cwd = Path(working_directory or self._env.working_directory)
        started = time.monotonic()
        timed_out = False
        notes: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="limbowave-sh-") as directory:
                script = Path(directory) / "script.sh"
                script.write_text(command, encoding="utf-8", newline="\n")
                args = [self._env.executable]
                if self._env.kind is ShellKind.BASH:
                    args += ["--noprofile", "--norc"]
                args.append(str(script))
                env = dict(os.environ)
                # Non-interactive shells must not silently source user startup files.
                for name in ("BASH_ENV", "ENV"):
                    env.pop(name, None)
                with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                    process = subprocess.Popen(
                        args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=err, start_new_session=True,
                    )
                    try:
                        process.wait(timeout=self._timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        notes.append(f"命令在 {self._timeout} 秒内未完成，已终止进程组")
                    finally:
                        self._kill_tree(process.pid)
                        process.wait(timeout=10)
                    out.seek(0)
                    err.seek(0)
                    stdout, out_cut = self._decode(out.read(self._max_output + 1))
                    stderr, err_cut = self._decode(err.read(self._max_output + 1))
                if out_cut or err_cut:
                    notes.append("输出超过上限已截断")
                return ShellResult(
                    shell=self._env.kind.value, command_id=command_id,
                    working_directory=str(cwd),
                    exit_code=None if timed_out else process.returncode,
                    timed_out=timed_out, stdout=stdout, stderr=stderr,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    truncated=out_cut or err_cut, notes=tuple(notes),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return ShellResult(
                shell=self._env.kind.value, command_id=command_id,
                working_directory=str(cwd), exit_code=None, timed_out=timed_out,
                stdout="", stderr=f"无法执行 shell：{exc}",
                notes=("shell 启动或清理失败，未确认成功",),
            )
