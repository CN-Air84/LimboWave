from __future__ import annotations

from pathlib import Path

import pytest

from limbowave import __version__, bootstrap
from limbowave.bootstrap import AppPaths, create_context, ensure_directories, resolve_paths


def test_version_is_exposed() -> None:
    assert __version__ == "0.0.1"


def test_resolve_paths_is_deterministic() -> None:
    assert resolve_paths() == resolve_paths()


def test_resolve_paths_does_not_touch_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析路径不得有副作用，落盘只能由 ensure_directories 显式发起。"""
    target = tmp_path / "vault"
    monkeypatch.setattr(bootstrap, "user_data_dir", lambda *a, **k: str(target))
    monkeypatch.setattr(bootstrap, "user_log_dir", lambda *a, **k: str(target / "logs"))

    paths = bootstrap.resolve_paths()

    assert paths.data_root == target
    assert not target.exists()


def test_resolve_paths_omits_appauthor_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """platformdirs 在 Windows 上默认用 appname 顶替 appauthor，
    会生成 ``...\\Local\\LimboWave\\LimboWave`` 这样的重复层级。"""
    captured: dict[str, object] = {}

    def fake_user_data_dir(
        appname: str, appauthor: object = None, *args: object, **kwargs: object
    ) -> str:
        captured["appname"] = appname
        captured["appauthor"] = appauthor
        return str(tmp_path / "data")

    monkeypatch.setattr(bootstrap, "user_data_dir", fake_user_data_dir)

    resolve_paths()

    assert captured["appname"] == "LimboWave"
    assert captured["appauthor"] is False


def test_ensure_directories_creates_roots(tmp_path: Path) -> None:
    paths = AppPaths(data_root=tmp_path / "data", log_root=tmp_path / "logs")

    ensure_directories(paths)

    assert paths.data_root.is_dir()
    assert paths.log_root.is_dir()


def test_create_context_reports_runtime_facts() -> None:
    context = create_context()

    assert context.python_version.count(".") == 2
    assert context.frozen is False
    assert context.paths.data_root.is_absolute()


def test_chinese_paths_round_trip(tmp_path: Path) -> None:
    """中文资料库路径必须能建目录并读写 UTF-8 内容。"""
    paths = AppPaths(data_root=tmp_path / "资料库" / "灵波", log_root=tmp_path / "日志")
    ensure_directories(paths)

    text = "# 灵波\n中文路径下的写入与读取。\n"
    payload = paths.data_root / "会话.md"
    payload.write_text(text, encoding="utf-8")

    assert payload.read_text(encoding="utf-8") == text
