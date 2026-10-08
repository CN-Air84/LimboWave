"""应用引导：解析运行环境事实与路径，不建立任何业务状态。

权威业务状态由 domain / application 层持有。GUI 与 Pi Runtime 都不从这里取状态，
本模块只回答"这次进程跑在什么环境里"。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "LimboWave"
APP_DISPLAY_NAME = "LimboWave / 灵波"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """资料库根目录与日志目录。此处只解析，不创建。"""

    data_root: Path
    log_root: Path


@dataclass(frozen=True, slots=True)
class AppContext:
    """一次启动中解析出的环境事实。"""

    paths: AppPaths
    python_version: str
    frozen: bool

    @property
    def development(self) -> bool:
        return not self.frozen and is_development_environment()


def is_development_environment() -> bool:
    """Only an unfrozen source checkout enables developer-only entry points."""
    if getattr(sys, "frozen", False):
        return False
    source = Path(__file__).resolve()
    root = source.parents[2]
    return (
        source == root / "src" / "limbowave" / "bootstrap.py"
        and (root / "pyproject.toml").is_file()
        and (root / ".git").exists()
    )


def resolve_paths() -> AppPaths:
    """解析路径但不落盘，便于测试注入临时目录。

    platformdirs 在 Windows 上未指定 appauthor 时会拿 appname 顶替，
    产生 ``LimboWave\\LimboWave`` 这样的重复层级，故显式传 ``appauthor=False``。
    """
    return AppPaths(
        data_root=Path(user_data_dir(APP_NAME, appauthor=False)),
        log_root=Path(user_log_dir(APP_NAME, appauthor=False)),
    )


def create_context() -> AppContext:
    info = sys.version_info
    return AppContext(
        paths=resolve_paths(),
        python_version=f"{info.major}.{info.minor}.{info.micro}",
        frozen=bool(getattr(sys, "frozen", False)),
    )


def ensure_directories(paths: AppPaths) -> None:
    """显式创建目录。与 :func:`resolve_paths` 分开，调用方自己决定何时落盘。"""
    paths.data_root.mkdir(parents=True, exist_ok=True)
    paths.log_root.mkdir(parents=True, exist_ok=True)
