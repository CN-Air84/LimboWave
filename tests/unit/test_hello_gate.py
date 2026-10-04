"""Windows Hello 闸门（Task 1.3）：PowerShell + WinRT 投影那条路。

为什么值得单独测：这条路让「不引入 winrt/winsdk 依赖」与「真有 Hello 闸门」
同时成立——是本机实际生效的实现。测试分三层：

1. **纯函数**：脚本输出的解析（结果行 / 错误行 / 最后一条优先）。
2. **契约**：随包脚本本身的性质（纯 ASCII、只输出两种前缀、不碰认证内容）
   与调用方式（理由走环境变量——这是中文能安全传进去的原因）。
3. **真实可用性探测**：真的起一次 PowerShell 问系统「Hello 可用吗」。
   **不触发任何对话框**（check 模式无 UI），因此适合自动化。
   真正的 PIN 输入无法自动化——那一步由人完成，测试不假装覆盖了它。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from limbowave.infrastructure.crypto import recovery
from limbowave.infrastructure.crypto.recovery import (
    HELLO_ERROR_PREFIX,
    HELLO_RESULT_PREFIX,
    HelloOutcome,
    NoHelloGate,
    PowerShellHelloGate,
    _parse_hello_output,
    _run_hello_script,
    hello_available,
    hello_gate,
    reset_hello_cache,
)

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Hello 只在 Windows 上存在")


def _constant(outcome: HelloOutcome) -> object:
    """打桩用：不管怎么调都返回同一个结果。"""

    def fake(*_args: object, **_kwargs: object) -> HelloOutcome:
        return outcome

    return fake

# 系统可能回的所有可用性枚举值（来自 Windows.Security.Credentials.UI）
KNOWN_AVAILABILITY = {
    "Available",
    "DeviceNotPresent",
    "NotConfiguredForUser",
    "DisabledByPolicy",
    "DeviceBusy",
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """每个测试都从干净的探测缓存开始。"""
    reset_hello_cache()
    yield
    reset_hello_cache()


# ---------- 1. 解析 ----------


def test_parse_result_line() -> None:
    outcome = _parse_hello_output(f"some noise\n{HELLO_RESULT_PREFIX}Verified\n")
    assert outcome.result == "Verified"
    assert outcome.error is None
    assert outcome.ok


def test_parse_error_line() -> None:
    outcome = _parse_hello_output(f"{HELLO_ERROR_PREFIX}timeout\n")
    assert outcome.result is None
    assert outcome.error == "timeout"
    assert not outcome.ok


def test_parse_takes_last_marker() -> None:
    """脚本可能先打过别的行：以后出现的标记为准。"""
    text = f"{HELLO_ERROR_PREFIX}no-op\n{HELLO_RESULT_PREFIX}Available\n"
    assert _parse_hello_output(text).result == "Available"


def test_parse_ignores_unrelated_output() -> None:
    outcome = _parse_hello_output("随便什么输出\n没有任何标记\n")
    assert outcome.error == "no-output"
    assert not outcome.ok


def test_parse_empty_output() -> None:
    assert _parse_hello_output("").error == "no-output"


def test_outcome_ok_only_for_verified() -> None:
    """取消、超时、重试耗尽都不算通过——fail closed。"""
    for name in ("Canceled", "RetriesExhausted", "DeviceBusy", "NotConfiguredForUser"):
        assert not HelloOutcome(result=name).ok, name
    assert HelloOutcome(result="Verified").ok


def test_outcome_describe_never_hides_failure() -> None:
    assert HelloOutcome(result="Canceled").describe() == "Canceled"
    assert "timeout" in HelloOutcome(error="timeout").describe()


# ---------- 2. 脚本契约 ----------


def test_shipped_script_exists_and_is_ascii() -> None:
    """纯 ASCII 是硬要求：PS 5.1 按 ANSI 解析无 BOM 的非 ASCII 脚本。"""
    path = recovery._HELLO_SCRIPT_PATH
    assert path.is_file(), f"脚本应随包分发：{path}"
    raw = path.read_bytes()
    raw.decode("ascii")  # 抛 UnicodeDecodeError 即失败
    assert not raw.startswith(b"\xef\xbb\xbf"), "不应有 BOM"


def test_shipped_script_only_emits_the_two_prefixes() -> None:
    """脚本只能输出结果/错误两种前缀——不许多打别的东西。"""
    text = recovery._HELLO_SCRIPT_PATH.read_text(encoding="ascii")
    for line in text.splitlines():
        if "Write-Output" not in line:
            continue
        assert HELLO_RESULT_PREFIX.strip() in line or HELLO_ERROR_PREFIX.strip() in line, line


def test_shipped_script_never_reads_credentials() -> None:
    """去掉注释后，脚本里不得有任何读取凭据的调用。

    只检查**代码**（注释里出现 "PIN" 是在解释设计，不是违规）：
    读凭据的 cmdlet 与读环境变量的白名单各查一遍。
    """
    script = recovery._HELLO_SCRIPT_PATH.read_text(encoding="ascii")
    code = chr(10).join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    ).lower()
    for forbidden in (
        "get-credential",
        "convertto-securestring",
        "convertfrom-securestring",
        "read-host",
        "-asplaintext",
        "logonuser",
    ):
        assert forbidden not in code, f"脚本不该涉及 {forbidden}"

    # 只允许读我们约定的三个环境变量（多读一个是越界）
    import re

    used = set(re.findall(r"\$env:([A-Za-z_]+)", script))
    assert used == {
        "LIMBOWAVE_HELLO_MODE",
        "LIMBOWAVE_HELLO_REASON",
        "LIMBOWAVE_HELLO_TIMEOUT_MS",
    }, used


def test_script_is_run_without_profiles_or_policy_friction() -> None:
    """运行方式固定：无 profile、绕过执行策略、单线程单元。"""
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{HELLO_RESULT_PREFIX}Verified\n", stderr=""
        )

    original = recovery.subprocess.run
    recovery.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        _run_hello_script("verify", reason="重置主密码", timeout_ms=1000)
    finally:
        recovery.subprocess.run = original  # type: ignore[assignment]

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "-NoProfile" in argv and "-NonInteractive" in argv and "-STA" in argv
    assert "-ExecutionPolicy" in argv and "Bypass" in argv
    assert str(recovery._HELLO_SCRIPT_PATH) in argv


def test_reason_travels_via_environment_not_the_script() -> None:
    """中文理由必须走环境变量：这是它不经过脚本编码那一关的原因。

    （Phase 7 实测：PS 5.1 读无 BOM 的 UTF-8 非 ASCII 脚本会按 ANSI 解析而报错。）
    """
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs.get("env")
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    original = recovery.subprocess.run
    recovery.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        _run_hello_script("verify", reason="重置 LimboWave 主密码", timeout_ms=1000)
    finally:
        recovery.subprocess.run = original  # type: ignore[assignment]

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["LIMBOWAVE_HELLO_REASON"] == "重置 LimboWave 主密码"
    assert env["LIMBOWAVE_HELLO_MODE"] == "verify"
    assert env["LIMBOWAVE_HELLO_TIMEOUT_MS"] == "1000"
    # 脚本文件里不该出现理由文本
    assert "重置" not in recovery._HELLO_SCRIPT_PATH.read_text(encoding="ascii")


def test_missing_script_is_reported_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "_HELLO_SCRIPT_PATH", Path("does-not-exist.ps1"))
    monkeypatch.setattr(recovery, "_find_powershell", lambda: "powershell.exe")
    assert _run_hello_script("check", reason="", timeout_ms=100).error == "no-script"


def test_no_powershell_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "_find_powershell", lambda: None)
    assert _run_hello_script("check", reason="", timeout_ms=100).error == "no-powershell"


def test_spawn_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """起进程失败不能让异常冒到解锁流程里——返回失败即可。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("spawn 失败")

    monkeypatch.setattr(recovery.subprocess, "run", boom)
    assert _run_hello_script("check", reason="", timeout_ms=100).error == "spawn-failed"


def test_subprocess_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=1)

    monkeypatch.setattr(recovery.subprocess, "run", slow)
    assert _run_hello_script("verify", reason="x", timeout_ms=100).error == "killed"


# ---------- 3. 闸门行为（探测被打桩） ----------


def test_gate_verify_true_only_on_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "hello_available", lambda: True)
    gate = PowerShellHelloGate()

    monkeypatch.setattr(
        recovery, "_run_hello_script", lambda *a, **k: HelloOutcome(result="Verified")
    )
    assert gate.verify("理由")

    for other in ("Canceled", "RetriesExhausted", "DeviceBusy"):
        monkeypatch.setattr(recovery, "_run_hello_script", _constant(HelloOutcome(result=other)))
        assert not gate.verify("理由"), other


def test_gate_verify_false_when_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "hello_available", lambda: True)
    monkeypatch.setattr(recovery, "_run_hello_script", _constant(HelloOutcome(error="timeout")))
    assert not PowerShellHelloGate().verify("理由")


def test_gate_verify_does_not_prompt_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """不可用时连脚本都不该起（更不能弹窗）。"""
    calls: list[str] = []
    monkeypatch.setattr(recovery, "hello_available", lambda: False)
    monkeypatch.setattr(
        recovery,
        "_run_hello_script",
        lambda *a, **k: calls.append("ran") or HelloOutcome(result="Verified"),
    )
    assert not PowerShellHelloGate().verify("理由")
    assert calls == []


def test_gate_name() -> None:
    assert PowerShellHelloGate().name == "windows-hello"


# ---------- 4. 可用性探测与缓存 ----------


def test_availability_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(mode: str, **_kwargs: object) -> HelloOutcome:
        calls.append(mode)
        return HelloOutcome(result="Available")

    monkeypatch.setattr(recovery, "_run_hello_script", fake)
    assert hello_available() is True
    assert hello_available() is True
    assert calls == ["check"], "探测结果应被缓存，且只探测一次"

    reset_hello_cache()
    assert hello_available() is True
    assert calls == ["check", "check"], "清缓存后应重新探测"


def test_availability_false_for_other_enums(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in sorted(KNOWN_AVAILABILITY - {"Available"}):
        reset_hello_cache()
        monkeypatch.setattr(
            recovery, "_run_hello_script", lambda *a, _n=name, **k: HelloOutcome(result=_n)
        )
        assert hello_available() is False, name


def test_gate_falls_back_to_account_level_without_hello(monkeypatch: pytest.MonkeyPatch) -> None:
    """两条路都不通时退回账户级——并让上层能看出来。"""
    from limbowave.domain import platform_capabilities
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: True)
    monkeypatch.setattr(recovery, "_winrt_available", lambda: False)
    monkeypatch.setattr(recovery, "_find_powershell", lambda: None)
    assert isinstance(hello_gate(), NoHelloGate)


def test_gate_uses_powershell_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from limbowave.domain import platform_capabilities
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: True)
    monkeypatch.setattr(recovery, "_winrt_available", lambda: False)
    monkeypatch.setattr(recovery, "_find_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(recovery, "hello_available", lambda: True)
    assert isinstance(hello_gate(), PowerShellHelloGate)


def test_winrt_binding_wins_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """装了 Python 绑定时优先用它（省一次进程）。"""
    from limbowave.domain import platform_capabilities
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: True)
    monkeypatch.setattr(recovery, "_winrt_available", lambda: True)
    assert isinstance(hello_gate(), recovery.WinRtHelloGate)


# ---------- 5. 真实可用性探测（无 UI） ----------


@WINDOWS_ONLY
def test_real_availability_probe_reaches_the_system() -> None:
    """真起一次 PowerShell 问系统。

    **不断言结果是 Available**——没配 Hello 的机器应回 NotConfiguredForUser，
    那也是这条链路工作的证据。断言的是「拿到了系统回的枚举值」，
    即类型加载、AsTask 包装、泛型反射这一整套真的走通了。
    """
    outcome = _run_hello_script("check", reason="", timeout_ms=recovery.AVAILABILITY_TIMEOUT_MS)
    assert outcome.error is None, f"脚本侧失败：{outcome.describe()}"
    assert outcome.result in KNOWN_AVAILABILITY, outcome.result


@WINDOWS_ONLY
def test_real_gate_is_self_consistent() -> None:
    """闸门的 available() 必须与真实探测一致（不许乐观也不许悲观）。"""
    gate = PowerShellHelloGate()
    probe = _run_hello_script("check", reason="", timeout_ms=recovery.AVAILABILITY_TIMEOUT_MS)
    assert gate.available() is (probe.result == "Available")
