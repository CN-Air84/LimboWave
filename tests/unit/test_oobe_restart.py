"""OOBE restarts retain launch options without exposing secrets or using a shell."""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from limbowave.infrastructure import restart


def test_restart_command_preserves_options_and_adds_oobe_once(monkeypatch) -> None:
    launch = Mock()
    monkeypatch.setattr(restart.subprocess, "Popen", launch)
    monkeypatch.setattr(sys, "executable", "C:/App With Spaces/LimboWave.exe")
    monkeypatch.setenv("LIMBOWAVE_TEST_MARKER", "keep")
    restart.restart_into_oobe(
        ["old-entry", "--log-level", "DEBUG", "--log-dir", "C:/Logs Here", "--oobe"],
        frozen=False,
    )
    command = launch.call_args.args[0]
    prefix = [sys.executable, "-m", "limbowave"]
    assert command == [*prefix, "--log-level", "DEBUG", "--log-dir", "C:/Logs Here", "--oobe"]
    options = launch.call_args.kwargs
    assert options.get("shell", False) is False
    assert options["env"]["LIMBOWAVE_TEST_MARKER"] == "keep"
    if sys.platform == "win32":
        assert options["creationflags"] & restart.subprocess.CREATE_NO_WINDOW


def test_relaunch_failure_is_not_reported_as_success(monkeypatch) -> None:
    monkeypatch.setattr(restart.subprocess, "Popen", Mock(side_effect=OSError("unavailable")))
    with pytest.raises(OSError, match="unavailable"):
        restart.restart_into_oobe(["limbowave"], frozen=False)


@pytest.mark.parametrize(("frozen", "development"), [(True, True), (False, False)])
def test_release_cannot_launch_oobe_debug_restart(monkeypatch, frozen, development) -> None:
    launch = Mock()
    monkeypatch.setattr(restart.subprocess, "Popen", launch)
    monkeypatch.setattr(restart, "is_development_environment", lambda: development)
    with pytest.raises(PermissionError, match="仅限开发环境"):
        restart.restart_into_oobe(["limbowave", "--oobe"], frozen=frozen)
    launch.assert_not_called()
