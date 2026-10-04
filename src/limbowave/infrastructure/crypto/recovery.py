"""系统保护恢复封装（Task 1.3 / 设计计划 §12.3）。

目标：**忘记主密码时仍能取回资料库**，而不必保留一份明文密钥。

实现分两层，各司其职：

1. **密钥保护层 = Windows DPAPI**（``CryptProtectData``）。它把主密钥加密成
   只能由**同一个 Windows 用户**解开的 blob。这是真正提供保护的机制——
   不需要新依赖，用 ctypes 直接调系统 API。
2. **身份确认层 = Windows Hello**（可选闸门）。DPAPI 只绑定「Windows 用户」，
   不要求生物识别/PIN。若希望「取回密钥前先验一次脸/PIN」，就在释放密钥前
   调一次 Hello 验证（``UserConsentVerifier``）。

**如实说明的现状**：Hello 闸门有两条实现路径，按可用性依次尝试：

1. ``winrt`` / ``winsdk`` Python 绑定（本项目**未**引入该依赖，若用户环境碰巧
   装了就用）；
2. **Windows PowerShell 5.1 + WinRT 投影**——Windows 自带，不需要任何新依赖。
   这是本机实际生效的那条路（见 ``PowerShellHelloGate``）。

两条都不可用（或系统未配置 Hello）时，**明确告知用户当前只受 Windows 账户保护**，
绝不假装有生物识别。

**安全取舍（必须让用户知道）**：启用恢复后，任何能进入该 Windows 会话的人
都能重置资料库主密码。这正是设计计划 §1.3 要求「支持主动关闭恢复」的原因——
在意这一点的用户可以关掉它，回到纯主密码保护。

**PIN 与认证内容**：本模块从头到尾**不接触** PIN 或生物特征数据——验证由系统
完成，只返回「通过/未通过」。因此「日志中不得记录 PIN 或认证内容」是
**结构性成立**的，不是靠自觉。PowerShell 路径同样只读回结果枚举名。
"""

from __future__ import annotations

import base64
import ctypes
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# DPAPI 的附加熵：把本应用的密文与其他应用区分开（同一用户下也互不可解）
_ENTROPY = b"limbowave.vault.recovery.v1"

# 包装方法标识（写进 vault.json 的 wraps[].method）
METHOD_DPAPI = "dpapi"


class RecoveryError(Exception):
    """恢复封装不可用或操作失败。"""


class RecoveryUnavailable(RecoveryError):
    """当前平台/环境不支持系统保护（如非 Windows，或 DPAPI 调用失败）。"""


class HelloGate(Protocol):
    """身份确认闸门。返回是否通过。

    实现**不得**记录或返回任何认证内容——只有布尔结果。
    """

    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def verify(self, reason: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class UnavailableHelloGate:
    """No native identity backend; never silently authorize an operation."""

    @property
    def name(self) -> str:
        return "unavailable"

    def available(self) -> bool:
        return False

    def verify(self, reason: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class NoHelloGate:
    """无 Hello 时的闸门：直接通过（DPAPI 已提供账户级保护）。

    它的存在意义是让上层与用户都能看出「这一步没有生物识别」——
    ``describe()`` 会明说当前保护等级。
    """

    @property
    def name(self) -> str:
        return "windows-account"

    def available(self) -> bool:
        return True

    def verify(self, reason: str) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class WinRtHelloGate:
    """Windows Hello 闸门（WinRT ``UserConsentVerifier``）。

    仅在运行时能导入 WinRT 绑定时构造。**只取布尔结果，不取任何认证内容。**
    """

    @property
    def name(self) -> str:
        return "windows-hello"

    def available(self) -> bool:
        return _winrt_available()

    def verify(self, reason: str) -> bool:
        if not self.available():
            return False
        try:
            import asyncio

            from winrt.windows.security.credentials.ui import (  # type: ignore[import-not-found]
                UserConsentVerifier,
                UserConsentVerifierAvailability,
            )

            availability = asyncio.run(UserConsentVerifier.check_availability_async())
            if availability != UserConsentVerifierAvailability.AVAILABLE:
                return False
            result = asyncio.run(UserConsentVerifier.request_verification_async(reason))
            # 只比较结果枚举，绝不读取/记录任何认证内容
            return str(result).endswith("VERIFIED")
        except Exception:
            return False


def _winrt_available() -> bool:
    try:
        import winrt.windows.security.credentials.ui  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# Windows PowerShell 路径：不引入新依赖的 Hello 闸门
# --------------------------------------------------------------------------

# Hello 脚本随包分发（``hello_check.ps1``），而不是内嵌在 Python 字符串里：
# 它是一段能被当 PowerShell 读的代码，独立成文件比嵌在字符串里更可维护，
# 也**天然回避了 PS 5.1 的编码陷阱**（文件是纯 ASCII，无 BOM 问题）。
#
# 脚本只回两种行：``LIMBOWAVE_HELLO_RESULT <枚举名>`` /
# ``LIMBOWAVE_HELLO_ERROR <原因>``，**绝不回任何认证内容**。
_HELLO_SCRIPT_PATH = Path(__file__).resolve().parent / "hello_check.ps1"

HELLO_RESULT_PREFIX = "LIMBOWAVE_HELLO_RESULT "
HELLO_ERROR_PREFIX = "LIMBOWAVE_HELLO_ERROR "
# 可用性探测：不需要用户操作，给短超时
AVAILABILITY_TIMEOUT_MS = 20_000
# 验证：用户要在系统对话框上打 PIN/按指纹，给足时间
VERIFY_TIMEOUT_MS = 90_000

# 进程级缓存：探测一次就够（每次探测都要起一个 PowerShell 进程，不是免费的）
_availability_cache: bool | None = None


def reset_hello_cache() -> None:
    """清掉可用性缓存。测试用（也让用户在配置 Hello 后不必重启应用即可刷新）。"""
    global _availability_cache
    _availability_cache = None


def _find_powershell() -> str | None:
    """找一个可用的 Windows PowerShell。优先 5.1（系统自带，WinRT 投影可靠）。"""
    if os.name != "nt":
        return None
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


@dataclass(frozen=True, slots=True)
class HelloOutcome:
    """一次 Hello 调用的结果。**只有枚举名或脚本侧错误码，没有认证内容。**

    ``result`` 与 ``error`` 互斥：成功拿到结果时 ``error`` 为 None，反之亦然。
    """

    result: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "Verified"

    def describe(self) -> str:
        if self.result is not None:
            return self.result
        return f"未取得结果（{self.error or '未知'}）"


def _parse_hello_output(text: str) -> HelloOutcome:
    """解析脚本输出。取**最后一条**标记行（脚本可能先打过别的行）。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith(HELLO_RESULT_PREFIX):
            return HelloOutcome(result=line[len(HELLO_RESULT_PREFIX) :].strip())
        if line.startswith(HELLO_ERROR_PREFIX):
            return HelloOutcome(error=line[len(HELLO_ERROR_PREFIX) :].strip())
    return HelloOutcome(error="no-output")


def _run_hello_script(mode: str, *, reason: str, timeout_ms: int) -> HelloOutcome:
    """跑一次 Hello 脚本。

    ``mode`` 为 ``"check"``（探测可用性，不弹窗）或 ``"verify"``（弹系统对话框）。
    用户不输入时 verify 会得到 ``error="timeout"``——与「取消」区分开，
    便于测试与人排查，但**两者对调用方都是不通过**（fail closed）。
    """
    shell = _find_powershell()
    if shell is None:
        return HelloOutcome(error="no-powershell")

    script = _HELLO_SCRIPT_PATH
    if not script.is_file():
        return HelloOutcome(error="no-script")

    env = dict(os.environ)
    env["LIMBOWAVE_HELLO_MODE"] = mode
    env["LIMBOWAVE_HELLO_REASON"] = reason
    env["LIMBOWAVE_HELLO_TIMEOUT_MS"] = str(timeout_ms)
    try:
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_ms / 1000 + 15,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HelloOutcome(error="killed")
    except (OSError, subprocess.SubprocessError):
        return HelloOutcome(error="spawn-failed")
    return _parse_hello_output(completed.stdout or "")


def hello_available() -> bool:
    """这台机器现在是否能用 Hello 确认身份（缓存一次）。"""
    global _availability_cache
    if _availability_cache is None:
        outcome = _run_hello_script(
            "check", reason="", timeout_ms=AVAILABILITY_TIMEOUT_MS
        )
        _availability_cache = outcome.result == "Available"
    return _availability_cache


@dataclass(frozen=True, slots=True)
class PowerShellHelloGate:
    """用系统自带的 PowerShell 调 WinRT ``UserConsentVerifier``。

    走这条路的原因：本项目不依赖 winrt/winsdk，而 Windows 自带的
    Windows PowerShell 5.1 能直接投影 WinRT 类型——于是**不引入新依赖**也能
    拿到真实的 Hello 闸门（PIN 与生物识别由系统决定用哪个，两者都算通过）。
    """

    @property
    def name(self) -> str:
        return "windows-hello"

    def available(self) -> bool:
        return hello_available()

    def verify(self, reason: str) -> bool:
        if not self.available():
            return False
        # 闸门只回枚举名，认证内容（PIN/指纹）从未离开系统。
        # 取消、超时、重试耗尽都落到 False —— 一律 fail closed。
        return _run_hello_script(
            "verify", reason=reason, timeout_ms=VERIFY_TIMEOUT_MS
        ).ok


def hello_gate() -> HelloGate:
    """挑一个可用的身份确认闸门。没有 Hello 就退回账户级（并如实标注）。"""
    from limbowave.domain.platform_capabilities import supports_system_identity

    if not supports_system_identity():
        return UnavailableHelloGate()
    if _winrt_available():
        return WinRtHelloGate()
    if _find_powershell() is not None:
        gate = PowerShellHelloGate()
        if gate.available():
            return gate
    return NoHelloGate()


def describe_protection() -> str:
    """给用户看的一句话：当前恢复封装的实际保护等级。"""
    from limbowave.domain.platform_capabilities import (
        SYSTEM_RECOVERY_UNAVAILABLE,
        supports_system_identity,
    )

    if not supports_system_identity():
        return SYSTEM_RECOVERY_UNAVAILABLE
    gate = hello_gate()
    if gate.name == "windows-hello":
        return (
            "系统保护已启用：主密钥由 Windows 账户保护，取回时需要 Windows Hello 验证"
            "（PIN 或生物识别，由系统按本机配置决定；PIN 即可通过）。"
        )
    return (
        "系统保护已启用：主密钥由 Windows 账户保护，**未**要求 Windows Hello 验证"
        "（本机未配置 Windows Hello 或不可用）。任何能进入该 Windows 会话的人"
        "都能重置主密码；在意这一点请关闭恢复。"
    )


# ---------- DPAPI（ctypes，无新依赖） ----------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def dpapi_available() -> bool:
    """DPAPI 是否可用（仅 Windows）。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.crypt32)
    except Exception:
        return False


def dpapi_protect(secret: bytes, *, description: str = "LimboWave vault key") -> bytes:
    """用当前 Windows 用户凭据保护一段数据。返回密文 blob。"""
    if not dpapi_available():
        raise RecoveryUnavailable("当前平台不支持 Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _blob(secret)
    entropy_blob = _blob(_ENTROPY)
    out_blob = _DataBlob()
    description_ptr = ctypes.c_wchar_p(description)

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        description_ptr,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN：不弹系统 UI
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RecoveryUnavailable(f"DPAPI 加密失败（错误码 {kernel32.GetLastError()}）")
    try:
        return _blob_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    """解开 DPAPI 密文。**换一个 Windows 用户就解不开**（这是保护本身）。"""
    if not dpapi_available():
        raise RecoveryUnavailable("当前平台不支持 Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _blob(blob)
    entropy_blob = _blob(_ENTROPY)
    out_blob = _DataBlob()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RecoveryError(
            "无法解开系统保护的密钥：可能换了 Windows 用户或系统凭据已重置"
            f"（错误码 {kernel32.GetLastError()}）"
        )
    try:
        return _blob_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def encode_blob(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def decode_blob(text: str) -> bytes:
    return base64.b64decode(text)
