from __future__ import annotations

import os
from pathlib import Path

import pytest

from limbowave.infrastructure.atomic import atomic_write_text


def test_atomic_write_replaces_content_without_leaving_temporaries(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    atomic_write_text(target, '{\n  "ok": true\n}\n')
    assert target.read_text(encoding="utf-8") == '{\n  "ok": true\n}\n'
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_replace_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secrets.json"
    target.write_text("old", encoding="utf-8")

    def fail(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []
