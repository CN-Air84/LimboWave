"""PowerShell 脚本编码约定守卫。

本机只有 Windows PowerShell 5.1（无 pwsh）。它用系统 ANSI 代码页解码无 BOM 的脚本文件，
含中文的脚本会直接解析失败；PowerShell 7 默认 UTF-8，且同样接受 BOM。
所以 scripts/*.ps1 统一要求 UTF-8 BOM + CRLF，两边都能正确执行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
PS1_FILES = sorted(SCRIPTS_DIR.glob("*.ps1"))
UTF8_BOM = b"\xef\xbb\xbf"


def test_ps1_scripts_exist() -> None:
    assert PS1_FILES, f"未在 {SCRIPTS_DIR} 找到任何 .ps1 脚本"


@pytest.mark.parametrize("path", PS1_FILES, ids=lambda path: path.name)
def test_ps1_has_utf8_bom(path: Path) -> None:
    assert path.read_bytes().startswith(UTF8_BOM), f"{path.name} 缺少 UTF-8 BOM"


@pytest.mark.parametrize("path", PS1_FILES, ids=lambda path: path.name)
def test_ps1_has_no_lone_lf(path: Path) -> None:
    raw = path.read_bytes()
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, f"{path.name} 存在 {lone_lf} 处裸 LF 行尾"
