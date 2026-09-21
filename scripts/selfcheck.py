"""环境自检：打印解释器、shell 与关键依赖版本。

独立成脚本而不是 ``python -c``：Windows PowerShell 5.1 向原生进程传参时会剥离引号，
内联代码会被破坏（设计计划 §10.3 因此要求优先使用脚本文件执行）。
"""

from __future__ import annotations

import importlib.metadata as metadata
import platform
import shutil
import sys

PACKAGES = (
    "PySide6",
    "qasync",
    "httpx",
    "pydantic",
    "cryptography",
    "argon2-cffi",
    "platformdirs",
    "markdown-it-py",
    "pygments",
    "pytest",
    "pytest-qt",
    "pytest-asyncio",
    "ruff",
    "mypy",
)


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main() -> int:
    print(f"python      {sys.version.split()[0]}")
    print(f"executable  {sys.executable}")
    print(f"platform    {platform.platform()}")
    print(f"frozen      {bool(getattr(sys, 'frozen', False))}")

    pwsh = shutil.which("pwsh")
    powershell = shutil.which("powershell")
    print(f"pwsh        {pwsh or '(未安装 —— 终端模式将回退 Windows PowerShell 并需在界面明示)'}")
    print(f"powershell  {powershell or '(未找到)'}")

    print()
    print("packages:")
    missing: list[str] = []
    for name in PACKAGES:
        version = _version(name)
        if version is None:
            missing.append(name)
            print(f"  {name:<16} MISSING")
        else:
            print(f"  {name:<16} {version}")

    if missing:
        print()
        print(f"缺少依赖：{', '.join(missing)}")
        return 1

    print()
    print("依赖齐全。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
