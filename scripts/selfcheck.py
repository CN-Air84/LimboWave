"""环境自检：打印解释器、shell 与关键依赖版本。

独立成脚本而不是 ``python -c``：Windows PowerShell 5.1 向原生进程传参时会剥离引号，
内联代码会被破坏（设计计划 §10.3 因此要求优先使用脚本文件执行）。
"""

from __future__ import annotations

import importlib.metadata as metadata
import platform
import shutil
import sys

# 运行期依赖：缺一个就跑不起来。
RUNTIME_PACKAGES = (
    "PySide6",
    "qasync",
    "httpx",
    "pydantic",
    "cryptography",
    "argon2-cffi",
    "platformdirs",
    "markdown-it-py",
    "pygments",
    "pypinyin",
)

# 开发依赖：只在开发/CI 需要。便携包里**本就不含**，因此不能算缺失
# （否则便携启动器的自检会把应用挡在门外——这是实跑才发现的问题）。
DEV_PACKAGES = ("pytest", "pytest-qt", "pytest-asyncio", "ruff", "mypy")


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    # --runtime：只检查运行期依赖（便携启动器用这个）
    runtime_only = "--runtime" in args

    print(f"python      {sys.version.split()[0]}")
    print(f"executable  {sys.executable}")
    print(f"platform    {platform.platform()}")
    print(f"frozen      {bool(getattr(sys, 'frozen', False))}")

    if sys.platform == "win32":
        pwsh = shutil.which("pwsh")
        powershell = shutil.which("powershell")
        print(f"pwsh        {pwsh or '(未找到，尝试 Windows PowerShell)'}")
        print(f"powershell  {powershell or '(未找到)'}")
    else:
        print(f"bash        {shutil.which('bash') or '(未找到，尝试 POSIX sh)'}")
        print(f"sh          {shutil.which('sh') or '(未找到，终端不可用)'}")
        print("recovery    系统保护恢复和应用内数据重置不可用；主密码与加密备份可用")

    print()
    print("运行期依赖:")
    missing: list[str] = []
    for name in RUNTIME_PACKAGES:
        version = _version(name)
        if version is None:
            missing.append(name)
            print(f"  {name:<16} MISSING")
        else:
            print(f"  {name:<16} {version}")

    optional_missing: list[str] = []
    if not runtime_only:
        print()
        print("开发依赖（可选）:")
        for name in DEV_PACKAGES:
            version = _version(name)
            if version is None:
                optional_missing.append(name)
                print(f"  {name:<16} 未安装")
            else:
                print(f"  {name:<16} {version}")

    if missing:
        print()
        print(f"缺少运行期依赖：{', '.join(missing)}")
        return 1

    print()
    print("运行期依赖齐全。")
    if not runtime_only and optional_missing:
        print(f"（开发依赖未装：{', '.join(optional_missing)}——仅影响开发命令）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
