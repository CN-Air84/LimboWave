"""安全边界的领域规则测试（Phase 6 的安全核心）。

三块：路径越权防护（Task 6.2）、联网边界（Task 6.3）、Bash 误用检测（§10.4）。
这些都是纯函数，安全属性用测试锁死比文档可靠。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from limbowave.domain.network_guard import NetworkBlocked, check_host, is_forbidden_address
from limbowave.domain.path_guard import (
    InvalidPath,
    OutsideAllowedRoot,
    PathEscape,
    resolve_within,
    safe_join,
    validate_path,
)
from limbowave.domain.shell_diagnostics import detect_bash_isms, has_bash_isms

# ---------- 路径越权防护 ----------


def test_normal_relative_path_within_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    assert resolve_within(root, "a.txt") == (root / "a.txt").resolve()


def test_dotdot_escape_rejected(tmp_path: Path) -> None:
    """``..`` 相对穿越必须被拒。"""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
    with pytest.raises(OutsideAllowedRoot):
        resolve_within(root, "../secret.txt")


def test_nested_dotdot_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    with pytest.raises(OutsideAllowedRoot):
        resolve_within(root, "sub/../../outside.txt")


def test_absolute_path_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("o", encoding="utf-8")
    with pytest.raises(OutsideAllowedRoot):
        resolve_within(root, str(outside))


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """符号链接穿越：授权目录内的链接指向外部——纯词法检查看不出来。"""
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("s", encoding="utf-8")
    link = root / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(OutsideAllowedRoot):
        resolve_within(root, "link/secret.txt")


def test_sibling_prefix_not_confused(tmp_path: Path) -> None:
    """``/safe`` 不该匹配 ``/safeevil``——用相对路径判断而非字符串前缀。"""
    root = tmp_path / "safe"
    root.mkdir()
    evil = tmp_path / "safeevil"
    evil.mkdir()
    (evil / "x.txt").write_text("e", encoding="utf-8")
    with pytest.raises(OutsideAllowedRoot):
        resolve_within(root, str(evil / "x.txt"))


def test_null_byte_rejected() -> None:
    with pytest.raises(InvalidPath, match="空字节"):
        validate_path("foo\x00.txt")


def test_drive_relative_rejected() -> None:
    with pytest.raises(InvalidPath, match="驱动器相对"):
        validate_path("C:foo", platform="win32")


@pytest.mark.parametrize("name", ["CON", "NUL", "com1", "LPT9", "aux.txt"])
def test_reserved_device_names_rejected(name: str) -> None:
    with pytest.raises(InvalidPath, match="保留名"):
        validate_path(name, platform="win32")


def test_empty_path_rejected() -> None:
    with pytest.raises(InvalidPath):
        validate_path("   ")


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    assert safe_join(root, "sub") == (root / "sub").resolve()
    with pytest.raises(PathEscape):
        safe_join(root, "..")
    with pytest.raises(InvalidPath):
        safe_join(root, "a/b")  # 段内不得含分隔符


# ---------- 联网边界 ----------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.1.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
    ],
)
def test_private_and_loopback_forbidden(host: str) -> None:
    forbidden, reason = is_forbidden_address(host)
    assert forbidden, f"{host} 应被禁止"
    assert reason


@pytest.mark.parametrize(
    "host", ["169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254"]
)
def test_cloud_metadata_forbidden(host: str) -> None:
    forbidden, reason = is_forbidden_address(host)
    assert forbidden
    assert "元数据" in reason


def test_public_address_allowed() -> None:
    forbidden, _ = is_forbidden_address("93.184.216.34")
    assert not forbidden


def test_domain_resolving_to_private_blocked() -> None:
    """域名解析到内网：防 DNS 指向内网。"""
    with pytest.raises(NetworkBlocked, match="解析到被禁止"):
        check_host("evil.example.com", resolver=lambda _host: ["127.0.0.1"])


def test_metadata_never_allowed_even_with_private_grant() -> None:
    """显式授权内网也不放开云元数据——SSRF 提权跳板。"""
    with pytest.raises(NetworkBlocked, match="始终禁止"):
        check_host("169.254.169.254", allow_private=True)


def test_private_allowed_with_explicit_grant() -> None:
    """用户显式授权内网时放行私有/环回。"""
    check_host("192.168.1.10", allow_private=True)
    check_host("127.0.0.1", allow_private=True)


def test_private_blocked_by_default() -> None:
    with pytest.raises(NetworkBlocked, match="私有网段"):
        check_host("192.168.1.10")


def test_unresolvable_host_blocked() -> None:
    with pytest.raises(NetworkBlocked, match="无法解析"):
        check_host("no-such-host.invalid", resolver=lambda _h: [])


def test_public_domain_passes() -> None:
    check_host("example.com", resolver=lambda _h: ["93.184.216.34"])


def test_metadata_hostname_blocked() -> None:
    with pytest.raises(NetworkBlocked, match="始终禁止"):
        check_host("metadata.google.internal")


# ---------- Bash 误用检测 ----------


@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ("export FOO=bar", "export"),
        ("FOO=bar python x.py", "inline_assign"),
        ("source ~/.bashrc", "source"),
        ("ls > /dev/null", "dev_null"),
        ("cat <<EOF\nhello\nEOF", "heredoc"),
        ("rm -rf /tmp/x", "rm_rf"),
        ("cp -r a b", "cp_mv_flags"),
        ("echo hi && echo bye", "bash_and_chain"),
        ("echo $!", "dollar_bang"),
    ],
)
def test_detects_bash_isms(command: str, pattern: str) -> None:
    found = {d.pattern for d in detect_bash_isms(command)}
    assert pattern in found, f"未检测到 {pattern}：{command}"


def test_clean_powershell_command_has_no_diagnostics() -> None:
    assert detect_bash_isms("Get-ChildItem -Path . | Select-Object Name") == []
    assert not has_bash_isms("$env:PATH = 'C:\\bin'; Write-Output 'ok'")


def test_diagnostics_carry_suggestion() -> None:
    """诊断必须给模型可执行的修正方向，而不是只报错。"""
    diagnostics = detect_bash_isms("export FOO=bar")
    assert diagnostics
    assert diagnostics[0].suggestion
    assert "$env:" in diagnostics[0].suggestion
