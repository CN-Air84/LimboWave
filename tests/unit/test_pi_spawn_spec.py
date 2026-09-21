"""build_spawn_spec 的单元测试（不启动真实进程，注入 node/cli 路径）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.infrastructure.pi_adapter import build_spawn_spec


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    cli = (
        tmp_path
        / "node_modules"
        / "@earendil-works"
        / "pi-coding-agent"
        / "dist"
        / "bundle"
        / "cli.js"
    )
    cli.parent.mkdir(parents=True)
    cli.write_text("// stub", encoding="utf-8")
    return cli


def test_spawn_spec_hardened_baseline(fake_cli: Path, tmp_path: Path) -> None:
    spec = build_spawn_spec(
        provider="mock",
        model_id="mock-model",
        cwd=tmp_path,
        node="node",
        cli_path=fake_cli,
    )
    argv = spec.argv
    # 硬化基线（合同 §八）
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert "--no-session" in argv
    assert "--no-approve" in argv
    assert "--no-context-files" in argv
    assert argv[argv.index("--provider") + 1] == "mock"
    assert argv[argv.index("--model") + 1] == "mock/mock-model"
    assert "-e" in argv  # 政策执行扩展


def test_spawn_spec_windows_path_normalized(fake_cli: Path, tmp_path: Path) -> None:
    """jiti 在 Windows 上对反斜杠路径不稳：扩展路径必须转为正斜杠。"""
    win_ext = tmp_path / "sub dir" / "ext.ts"
    spec = build_spawn_spec(
        provider="mock",
        model_id="mock-model",
        cwd=tmp_path,
        node="node",
        cli_path=fake_cli,
        policy_extension=win_ext,
    )
    ext_arg = spec.argv[spec.argv.index("-e") + 1]
    assert "\\" not in ext_arg
    assert "/" in ext_arg


def test_spawn_spec_env_disables_telemetry(fake_cli: Path, tmp_path: Path) -> None:
    spec = build_spawn_spec(
        provider="mock",
        model_id="mock-model",
        cwd=tmp_path,
        node="node",
        cli_path=fake_cli,
    )
    assert spec.env is not None
    assert spec.env.get("PI_SKIP_VERSION_CHECK") == "1"
    assert spec.env.get("PI_OFFLINE") == "1"


def test_spawn_spec_extension_file_must_be_present() -> None:
    """默认政策扩展文件必须随包存在（否则权限网关不会加载，形成裸奔）。"""
    from limbowave.infrastructure.pi_adapter import _POLICY_EXTENSION

    assert _POLICY_EXTENSION.is_file(), f"政策扩展缺失：{_POLICY_EXTENSION}"
