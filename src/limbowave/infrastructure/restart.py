"""Relaunch the GUI after its old event loop, storage and diagnostics have closed."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from limbowave.bootstrap import is_development_environment


def restart_into_oobe(argv: Sequence[str], *, frozen: bool) -> None:
    """Force onboarding for one launch, retaining options and the current data directory."""
    if frozen or not is_development_environment():
        raise PermissionError("OOBE 调试重启仅限开发环境")
    command = [sys.executable, "-m", "limbowave"]
    command.extend(argument for argument in argv[1:] if argument != "--oobe")
    command.append("--oobe")
    environment = os.environ.copy()
    subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
