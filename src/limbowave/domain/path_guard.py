"""路径越权防护（Task 6.2：防止越权路径穿越）。

攻击面（逐条堵住）：

- ``..`` 相对穿越：``safe/../../etc/passwd``；
- 绝对路径逃逸：``C:\\Windows\\System32`` 或 ``/etc/shadow``；
- **符号链接穿越**：授权目录内放一个指向外部的链接——纯词法检查看不出来，
  必须解析真实路径（``resolve``）；
- Windows 驱动器相对路径：``C:foo``（无分隔符，语义与 ``C:\\foo`` 不同）；
- UNC 路径：``\\\\server\\share``；
- 空字节注入：``foo\\x00.txt``；
- Windows 保留设备名：``CON``、``NUL``、``COM1`` 等（可能造成挂起或异常行为）。

设计：**先规范化再判断包含关系**，并且解析符号链接（``resolve``）。
判断用「规范化后的路径是否等于根或以根 + 分隔符开头」，不用字符串前缀
（``/safe`` 不该匹配 ``/safeevil``）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Windows 保留设备名（不区分大小写，可带扩展名）
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_NULL_BYTE = re.compile("\x00")


class PathEscape(Exception):
    """路径越界或非法。"""


class InvalidPath(PathEscape):
    """路径本身非法（空字节、保留名、驱动器相对路径等）。"""


class OutsideAllowedRoot(PathEscape):
    """解析后的真实路径不在允许的根目录内。"""


def validate_path(raw: str, *, platform: str | None = None) -> Path:
    """基础合法性校验（不含授权判定）。返回规范化 Path。非法抛 InvalidPath。"""
    if not raw or not raw.strip():
        raise InvalidPath("路径为空")
    if _NULL_BYTE.search(raw):
        raise InvalidPath("路径含空字节")
    # Windows 保留设备名
    name = Path(raw).name.split(".")[0].lower()
    if (platform or sys.platform) == "win32" and name in _RESERVED_NAMES:
        raise InvalidPath(f"路径使用系统保留名：{raw}")
    # 驱动器相对路径（C:foo）语义含混，一律拒绝
    if (platform or sys.platform) == "win32" and re.match(r"^[A-Za-z]:[^\\/]", raw):
        raise InvalidPath(f"驱动器相对路径不被接受：{raw}")
    return Path(raw)


def resolve_within(root: Path, raw: str) -> Path:
    """把 ``raw`` 解析为真实路径并确认它在 ``root`` 内。越界抛 OutsideAllowedRoot。

    ``resolve()`` 会展开 ``..`` 与**符号链接**——这是与纯词法检查的关键区别。
    对不存在的路径，``resolve`` 仍会规范化（Python 默认 strict=False）。
    """
    candidate = validate_path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError as exc:  # 路径过长/非法字符等
        raise InvalidPath(f"路径无法解析：{raw}") from exc
    if not _is_within(resolved, root_resolved):
        raise OutsideAllowedRoot(f"路径越界：{raw}")
    return resolved


def _is_within(candidate: Path, root: Path) -> bool:
    """规范化包含判断。用相对路径而非字符串前缀（``/safe`` 不匹配 ``/safeevil``）。"""
    if candidate == root:
        return True
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    # 相对路径不得以 .. 开头（防御性：resolve 后通常已不会出现）
    return not relative.parts or relative.parts[0] != os.pardir


def safe_join(root: Path, *parts: str) -> Path:
    """在根目录内拼一个子路径，逐段校验。用于「列目录」等按名字进入的场景。"""
    current = root
    for part in parts:
        if not part or part in (".", ".."):
            raise InvalidPath(f"非法路径段：{part!r}")
        if (
            _NULL_BYTE.search(part) or "/" in part
            or (sys.platform == "win32" and "\\" in part)
        ):
            raise InvalidPath(f"路径段不得含分隔符：{part!r}")
        candidate = current / part
        resolved = resolve_within(root, str(candidate))
        current = resolved
    return current
