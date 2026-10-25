from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.package_server import PackageError, payload_inventory, validate_wheel


def test_external_python_is_hashed_and_os_checked_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import subprocess
    import sys as current_sys
    from types import SimpleNamespace

    from scripts import install_server

    release = _release(tmp_path / "release")
    python = tmp_path / "portable-python"
    python.write_bytes(b"python")
    digest = hashlib.sha256(python.read_bytes()).hexdigest()
    (release / "external-python.json").write_text(
        json.dumps({"sha256": digest, "platform": current_sys.platform,
                    "machine": install_server.platform.machine(),
                    "version": install_server.subprocess.check_output(
                        [str(python), "--version"], text=True,
                    ).strip()}),
        encoding="utf-8",
    )
    release = _refresh_manifest(release)
    calls = []

    class Builder:
        def create(self, target):
            calls.append(("created", target))

    def fake_run(command, **kwargs):
        calls.append(("run", tuple(command)))

    def fake_output(command, **kwargs):
        return "Python 3.13.12"

    monkeypatch.setattr(install_server.venv, "EnvBuilder", lambda **kwargs: Builder())
    monkeypatch.setattr(install_server.subprocess, "run", fake_run)
    monkeypatch.setattr(install_server.subprocess, "check_output", fake_output)
    install_server.install_release(
        release, tmp_path / "new", python=python, python_checksum=release / "external-python.json",
    )
    assert calls[0][0] == "created"
    assert any(str(python) in command for _, command in calls if _ == "run")


def test_external_python_checksum_mismatch_fails_before_target_creation(tmp_path: Path) -> None:
    from scripts import install_server

    release = _release(tmp_path / "release")
    python = tmp_path / "python"
    python.write_bytes(b"actual")
    record = tmp_path / "python.json"
    record.write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="external Python checksum"):
        install_server.install_release(
            release, tmp_path / "new", python=python, python_checksum=record,
        )
    assert not (tmp_path / "new").exists()


