"""打包器核心逻辑的守卫：命令拼装、入口垫片与图标转换。

``scripts/packager.py`` 是脚本不是包成员，这里用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "packager.py"


def load_packager() -> ModuleType:
    spec = importlib.util.spec_from_file_location("limbowave_packager", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解时要回查 sys.modules，必须先注册再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_options(module: ModuleType, **overrides: object) -> object:
    return module.BuildOptions(**overrides)


def test_build_arguments_defaults(tmp_path: Path) -> None:
    packager = load_packager()
    options = packager.BuildOptions()
    args = packager.build_arguments(options, tmp_path, platform="win32")

    assert args[:4] == [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    assert "--clean" in args
    assert "--onefile" not in args  # 默认标准目录
    assert "--windowed" in args  # 默认隐藏 cmd
    assert "--noupx" in args  # 默认不启用 UPX，必须显式关闭
    assert "--upx-dir" not in args
    assert "--icon" not in args

    # 代码内相对加载的资源目录必须显式收集
    add_data = [args[i + 1] for i, item in enumerate(args) if item == "--add-data"]
    targets = {item.split(";", 1)[1] for item in add_data}
    assert targets == {
        "limbowave/ui/assets",
        "limbowave/infrastructure/extensions",
        "limbowave/infrastructure/crypto",
        "limbowave/web/static",
    }
    # about.json 未提供时不注入
    assert not any(item.endswith(";.") for item in add_data)

    # 入口垫片等价于 python -m limbowave
    entry = Path(args[-1])
    assert entry.parent == tmp_path
    assert "from limbowave.cli import main" in entry.read_text(encoding="utf-8")


def test_build_arguments_full_options(tmp_path: Path) -> None:
    packager = load_packager()
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"fake")
    about = tmp_path / "about.json"
    about.write_text("{}", encoding="utf-8")
    upx_dir = tmp_path / "upx"
    upx_dir.mkdir()
    dist = tmp_path / "out"

    options = packager.BuildOptions(
        name="灵波",
        onefile=True,
        hide_console=False,
        icon=icon,
        use_upx=True,
        upx_dir=upx_dir,
        about=about,
        dist_dir=dist,
    )
    args = packager.build_arguments(options, tmp_path / "work", platform="win32")

    assert "--onefile" in args
    assert "--windowed" not in args  # 显式保留控制台
    assert "--noupx" not in args
    assert args[args.index("--upx-dir") + 1] == str(upx_dir)
    assert args[args.index("--name") + 1] == "灵波"
    assert args[args.index("--icon") + 1] == str(icon)
    assert args[args.index("--distpath") + 1] == str(dist)
    assert f"{about};." in args


def test_exe_path_layout() -> None:
    packager = load_packager()
    onedir = packager.BuildOptions()
    onefile = packager.BuildOptions(onefile=True)
    assert packager.exe_path(onedir, platform="win32") == (
        onedir.dist_dir / "LimboWave" / "LimboWave.exe"
    )
    assert packager.exe_path(onefile, platform="win32") == onefile.dist_dir / "LimboWave.exe"


def test_cli_options_resolve_relative_paths(tmp_path: Path, monkeypatch) -> None:
    # PyInstaller 的 --add-data 按 --specpath 解析相对路径，CLI 必须先转绝对。
    packager = load_packager()
    monkeypatch.chdir(tmp_path)
    options = packager.options_from_cli(
        packager._parse_args([
            "--icon", "icon.png", "--about", "about.json",
            "--upx", "--upx-dir", "upx", "--dist", "out",
        ]),
    )
    assert options.icon == tmp_path / "icon.png"
    assert options.about == tmp_path / "about.json"
    assert options.upx_dir == tmp_path / "upx"
    assert options.dist_dir == tmp_path / "out"
    assert options.use_upx and options.hide_console  # 未传 --console，默认隐藏 cmd


def test_stage_about_renames_to_canonical_name(tmp_path: Path) -> None:
    # --add-data 保留源文件名，应用只认 about.json。
    packager = load_packager()
    custom = tmp_path / "custom-name.json"
    custom.write_text("{}", encoding="utf-8")
    staged = packager.stage_about(custom)
    assert staged.name == "about.json"
    assert staged.read_text(encoding="utf-8") == "{}"
    assert packager.stage_about(staged) == staged  # 同名不再复制


def test_prepare_icon_from_png(tmp_path: Path) -> None:
    packager = load_packager()
    source = tmp_path / "logo.png"
    Image.new("RGBA", (512, 512), (30, 144, 255, 255)).save(source)

    ico = packager.prepare_icon(source, tmp_path / "work")
    assert ico.name == "app.ico"
    with Image.open(ico) as image:
        assert image.size == (256, 256)  # 取到最大档


def test_prepare_icon_crops_non_square(tmp_path: Path) -> None:
    packager = load_packager()
    source = tmp_path / "wide.png"
    Image.new("RGBA", (600, 400), (200, 60, 60, 255)).save(source)

    ico = packager.prepare_icon(source, tmp_path / "work")
    with Image.open(ico) as image:
        assert image.size == (256, 256)  # 中心裁方后仍能取到 256 档


def test_prepare_icon_copies_ico(tmp_path: Path) -> None:
    packager = load_packager()
    Image.new("RGBA", (64, 64), (0, 0, 0, 255)).save(tmp_path / "src.ico")
    ico = packager.prepare_icon(tmp_path / "src.ico", tmp_path / "work")
    with Image.open(ico) as image:
        assert image.size == (64, 64)


def test_linux_build_arguments_and_output(tmp_path: Path) -> None:
    packager = load_packager()
    options = packager.BuildOptions(about=tmp_path / "about.json", icon=tmp_path / "icon.svg")
    args = packager.build_arguments(options, tmp_path / "work", platform="linux")
    assert "--windowed" not in args
    assert "--icon" not in args
    assert f"{options.about}:." in args
    assert all(
        value.endswith(":limbowave/" + target)
        for value, target in zip(
            [args[i + 1] for i, arg in enumerate(args) if arg == "--add-data"][:3],
            ["ui/assets", "infrastructure/extensions", "infrastructure/crypto"], strict=True,
        )
    )
    assert packager.exe_path(options, platform="linux").name == "LimboWave"
    single = packager.BuildOptions(onefile=True)
    assert packager.exe_path(single, platform="linux") == single.dist_dir / "LimboWave"

@pytest.fixture
def web_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """只在临时目录生成假产物，绝不调用 npm 或 PyInstaller。"""
    from unittest.mock import Mock

    packager = load_packager()
    web = tmp_path / "web"
    web.mkdir()
    for name in ("package.json", "package-lock.json"):
        (web / name).write_text("{}", encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    (static / "app.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setattr(packager, "WEB_DIR", web)
    monkeypatch.setattr(packager, "WEB_STATIC_DIR", static)
    monkeypatch.setattr(packager.shutil, "which", lambda _: "npm-test")
    processes = [Mock(stdout=["step log\n"], wait=Mock(return_value=0)) for _ in range(3)]
    popen = Mock(side_effect=processes)
    monkeypatch.setattr(packager.subprocess, "Popen", popen)
    return packager, processes, popen


def test_web_build_sequence_and_callbacks(web_build) -> None:
    packager, processes, popen = web_build
    logs, spawned = [], []
    packager.build_web_assets(logs.append, spawned.append)
    assert [call.args[0] for call in popen.call_args_list] == [
        ["npm-test", "ci"],
        ["npm-test", "run", "typecheck"],
        ["npm-test", "run", "build", "--", "--emptyOutDir"],
    ]
    assert all(call.kwargs["cwd"] == packager.WEB_DIR for call in popen.call_args_list)
    assert spawned == processes
    assert logs.count("step log") == 3


@pytest.mark.parametrize("step", [0, 1, 2])
def test_web_failure_blocks_pyinstaller(web_build, monkeypatch, tmp_path, step) -> None:
    packager, processes, popen = web_build
    processes[step].wait.return_value = 7
    monkeypatch.setattr(packager, "_missing_dependency", lambda: None)
    with pytest.raises(packager.BuildError, match=r"npm .*退出码 7"):
        packager.run_build(packager.BuildOptions(), tmp_path / "work")
    assert popen.call_count == step + 1
    assert not (tmp_path / "work" / "entry_app.py").exists()


def test_web_missing_npm(web_build, monkeypatch) -> None:
    packager, _, popen = web_build
    monkeypatch.setattr(packager.shutil, "which", lambda _: None)
    with pytest.raises(packager.BuildError, match="未找到 npm"):
        packager.build_web_assets()
    popen.assert_not_called()


@pytest.mark.parametrize("filename", ["package.json", "package-lock.json"])
def test_web_requires_manifest_and_lock(web_build, filename) -> None:
    packager, _, popen = web_build
    (packager.WEB_DIR / filename).unlink()
    with pytest.raises(packager.BuildError, match=filename):
        packager.build_web_assets()
    popen.assert_not_called()


def test_web_spawn_error_is_build_error(web_build) -> None:
    packager, _, popen = web_build
    popen.side_effect = OSError("cannot start")
    with pytest.raises(packager.BuildError, match="无法启动 npm ci"):
        packager.build_web_assets()


@pytest.mark.parametrize("filename", ["index.html", "app.js"])
@pytest.mark.parametrize("empty", [False, True])
def test_web_requires_nonempty_output(web_build, filename, empty) -> None:
    packager, _, _ = web_build
    path = packager.WEB_STATIC_DIR / filename
    if empty:
        path.write_bytes(b"")
    else:
        path.unlink()
    with pytest.raises(packager.BuildError, match="前端构建未生成"):
        packager.build_web_assets()


def test_web_build_precedes_freezing(web_build, monkeypatch, tmp_path) -> None:
    from unittest.mock import Mock

    packager, processes, popen = web_build
    monkeypatch.setattr(packager, "_missing_dependency", lambda: None)
    options = packager.BuildOptions(dist_dir=tmp_path / "dist")
    output = packager.exe_path(options)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"mock executable")
    popen.side_effect = [*processes, Mock(stdout=[], wait=Mock(return_value=0))]
    assert packager.run_build(options, tmp_path / "work") == output
    assert popen.call_count == 4
    assert popen.call_args.args[0][:3] == [sys.executable, "-m", "PyInstaller"]


@pytest.mark.parametrize("platform,separator", [("win32", ";"), ("linux", ":")])
@pytest.mark.parametrize("onefile", [False, True])
def test_web_static_collected_for_all_bundle_modes(tmp_path, platform, separator, onefile):
    packager = load_packager()
    args = packager.build_arguments(
        packager.BuildOptions(onefile=onefile), tmp_path, platform=platform,
    )
    assert f"{packager.WEB_STATIC_DIR}{separator}limbowave/web/static" in args
