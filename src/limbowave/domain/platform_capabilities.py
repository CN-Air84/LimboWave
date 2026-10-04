"""Explicit platform capabilities; no security fallback to a weaker mechanism."""
from __future__ import annotations

import sys

SYSTEM_RECOVERY_UNAVAILABLE = (
    "当前平台不支持 Windows 系统保护恢复。请使用主密码解锁，并定期创建独立密码的加密备份。"
    "从 Windows 迁移的系统恢复封装不能在此使用；忘记主密码且没有可用备份时，无法恢复数据。"
)
DATA_RESET_UNAVAILABLE = (
    "当前平台尚无已验证的系统二次身份验证后端，应用内数据重置已禁用。"
    "不会降级为仅验证主密码或当前登录会话；请先备份重要数据。"
)


def supports_system_identity(*, platform: str | None = None) -> bool:
    """Platform support only; Windows still has to check DPAPI/Hello availability."""
    return (platform or sys.platform) == "win32"
