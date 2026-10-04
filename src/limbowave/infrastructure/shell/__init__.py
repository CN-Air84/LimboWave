"""Platform shell probe and execution backend selection."""
from limbowave.domain.shell import ShellEnvironment, ShellKind
from limbowave.infrastructure.shell.executor import PowerShellExecutor
from limbowave.infrastructure.shell.posix_executor import PosixShellExecutor
from limbowave.infrastructure.shell.probe import probe_shell

__all__ = ["PosixShellExecutor", "PowerShellExecutor", "create_executor", "probe_shell"]


def create_executor(environment: ShellEnvironment) -> PowerShellExecutor:
    if environment.kind in (ShellKind.BASH, ShellKind.SH):
        return PosixShellExecutor(environment)
    return PowerShellExecutor(environment)
