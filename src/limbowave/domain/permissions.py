"""权限模型的领域规则（Phase 6 / 设计计划 §十一）。

三层决策（§11.1/§11.2）：

1. **能力与资源范围**：授权记录同时包含「能做什么」与「对哪些资源」——
   文件读取允许的目录、文件写入允许的目录、终端的工作目录与命令类别、
   联网允许的域名、以及执行模式（内置工具 / 直接终端）。
2. **会话级持久授权**：授权按会话永久保留，重开会话自动恢复。用户可查看/撤销/缩小。
3. **高影响操作额外确认**：即使已有会话授权，删除大量文件、覆盖关键文件、
   读取凭据目录、向外部发布内容、明显超出既有范围、跨站点重提交**仍要二次确认**。

本模块是**纯规则**：不碰数据库、不碰文件系统。判定「能不能做」由
:func:`evaluate` 给出，落库与审计在应用服务层。
"""

from __future__ import annotations

import enum
import ntpath
import posixpath
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class Capability(enum.StrEnum):
    """能力种类。"""

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    TERMINAL = "terminal"
    NETWORK = "network"
    MEMORY_WRITE = "memory_write"
    CLOCK_READ = "clock_read"


class PermissionPreset(enum.StrEnum):
    """会话在输入区选择的权限档位。"""

    CHAT_ONLY = "chat_only"
    READ_ONLY = "read_only"
    FULL_ACCESS = "full_access"
    CUSTOM = "custom"


class RiskLevel(enum.StrEnum):
    """风险级别。"""

    NORMAL = "normal"
    HIGH = "high"  # 需要额外确认（§11.2）


class Decision(enum.StrEnum):
    """判定结果。"""

    ALLOW = "allow"  # 范围内且非高影响：直接放行
    CONFIRM = "confirm"  # 范围内但属高影响，或未授权但可询问：需用户确认
    DENY = "deny"  # 明确拒绝（越界且不可询问，或用户拒绝）


class ExecutionMode(enum.StrEnum):
    """执行模式（§9.1）。会话中必须清晰显示当前模式。"""

    BUILTIN_TOOLS = "builtin_tools"  # 内置工具模式（默认）
    DIRECT_TERMINAL = "direct_terminal"  # 直接终端模式


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """一次请求涉及的资源。字段按能力使用，不用到的留空。"""

    paths: tuple[str, ...] = ()  # 文件读写涉及的路径
    domains: tuple[str, ...] = ()  # 联网涉及的主机名
    command: str | None = None  # 终端命令
    working_directory: str | None = None  # 终端工作目录


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """一次工具调用请求。"""

    tool_name: str
    capability: Capability
    scope: ResourceScope = field(default_factory=ResourceScope)
    raw_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """一条会话级持久授权（§11.1）。"""

    id: str
    conversation_id: str
    capability: Capability
    created_at: datetime
    # 资源边界：读取/写入允许的目录前缀；联网允许的域名；终端允许的工作目录
    allowed_paths: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    allowed_working_directories: tuple[str, ...] = ()
    # 终端命令类别（如 "read_only"）；空 = 不限制类别
    command_classes: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class GrantScope:
    """确认框里「本会话允许」要建立的授权边界——用户点之前就该知道授出去的是什么。"""

    capability: Capability
    allowed_paths: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """一次判定结果。``matched_rule`` 与 ``reason`` 供 UI 与审计。"""

    decision: Decision
    risk: RiskLevel
    reason: str
    matched_rule: str


@dataclass(frozen=True, slots=True)
class PermissionAudit:
    """一次权限决策的审计记录（§11.3）。不可变，只追加。"""

    id: str
    conversation_id: str
    created_at: datetime
    tool_name: str
    capability: Capability
    matched_rule: str
    decision: Decision
    risk: RiskLevel
    user_confirmed: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    actual_paths: tuple[str, ...] = ()


# ---------- 高影响操作识别（§11.2） ----------

# 凭据/密钥目录：读取这些目录即使已授权也要二次确认
SENSITIVE_PATH_PATTERNS = (
    re.compile(r"(^|[\\/])\.ssh([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])\.aws([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])\.gnupg([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])\.config[\\/]gcloud([\\/]|$)", re.IGNORECASE),
    re.compile(r"\.env(\.|$)", re.IGNORECASE),
    re.compile(r"id_rsa|id_ed25519|\.pem$|\.pfx$|\.p12$", re.IGNORECASE),
    re.compile(r"credential|secret|password|token", re.IGNORECASE),
)

# 大批量删除的阈值：一次涉及这么多路径就计入「删除大量文件」
BULK_DELETE_THRESHOLD = 10

# 危险命令类别：可能造成不可逆破坏
DESTRUCTIVE_COMMAND = re.compile(
    r"\b(rm\s+-rf|rm\s+-r|Remove-Item\b.*-Recurse|del\s+/[sqf]|format\b|"
    r"mkfs|dd\s+if=|shutdown|Stop-Computer|diskpart)\b",
    re.IGNORECASE,
)


def classify_risk(request: ToolRequest) -> tuple[RiskLevel, str]:
    """判定一次请求是否属高影响操作。返回 (级别, 原因)。纯函数。"""
    if (
        request.capability is Capability.FILE_WRITE
        and request.raw_params.get("operation") == "delete"
        and len(request.scope.paths) >= BULK_DELETE_THRESHOLD
    ):
        return RiskLevel.HIGH, f"一次删除 {len(request.scope.paths)} 个路径"
    if request.capability in (Capability.FILE_READ, Capability.FILE_WRITE):
        for path in request.scope.paths:
            for pattern in SENSITIVE_PATH_PATTERNS:
                if pattern.search(path):
                    return RiskLevel.HIGH, f"涉及凭据或密钥路径：{path}"
    if (
        request.capability is Capability.TERMINAL
        and request.scope.command
        and DESTRUCTIVE_COMMAND.search(request.scope.command)
    ):
        return RiskLevel.HIGH, "命令含破坏性操作"
    if request.capability is Capability.NETWORK and request.raw_params.get("publish"):
        return RiskLevel.HIGH, "向外部服务发布内容"
    return RiskLevel.NORMAL, ""


def path_is_within(
    candidate: str, allowed_prefix: str, *, platform: str | None = None
) -> bool:
    """Native lexical containment; callers resolve symlinks before authorization.

    POSIX paths are case-sensitive; backslashes are literal filename characters.
    """
    if not candidate or not allowed_prefix:
        return False
    paths = ntpath if (platform or sys.platform) == "win32" else posixpath
    candidate_norm = paths.normcase(paths.normpath(candidate))
    allowed_norm = paths.normcase(paths.normpath(allowed_prefix))
    try:
        return bool(paths.commonpath((candidate_norm, allowed_norm)) == allowed_norm)
    except ValueError:
        return False


def domain_is_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """主机是否在授权域名内。支持子域：授权 ``example.com`` 覆盖 ``a.example.com``。"""
    host = host.strip().lower()
    for allowed in allowed_domains:
        allowed = allowed.strip().lower()
        if not allowed:
            continue
        if allowed == "*":
            return True
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def evaluate(
    request: ToolRequest,
    grants: list[PermissionGrant],
    *,
    mode: ExecutionMode = ExecutionMode.BUILTIN_TOOLS,
) -> PermissionDecision:
    """对一个请求给出判定。**纯函数**（授予列表由调用方从库里读出）。

    判定顺序（固定，可解释）：
    1. 直接终端模式下，内置工具的范围约束不适用——如实标记为需确认（§9.1：
       用户不能误以为直接终端仍受细粒度约束）。
    2. 高影响操作：即使在授权范围内也要求二次确认（§11.2）。
    3. 有匹配授权且在范围内：放行。
    4. 无匹配授权：要求确认（用户可授予本次或本会话）。
    """
    if request.capability is Capability.CLOCK_READ:
        return PermissionDecision(
            decision=Decision.ALLOW,
            risk=RiskLevel.NORMAL,
            reason="读取设备当前时间，无需资源授权",
            matched_rule="clock.read",
        )

    risk, risk_reason = classify_risk(request)

    if mode is ExecutionMode.DIRECT_TERMINAL and request.capability is not Capability.TERMINAL:
        return PermissionDecision(
            decision=Decision.CONFIRM,
            risk=RiskLevel.HIGH,
            reason="直接终端模式下工具范围约束不适用，需确认",
            matched_rule="mode.direct_terminal",
        )

    matching = [g for g in grants if g.capability is request.capability]
    if not matching:
        return PermissionDecision(
            decision=Decision.CONFIRM,
            risk=risk,
            reason="该能力尚无会话授权",
            matched_rule="no_grant",
        )

    in_scope: PermissionGrant | None = None
    for grant in matching:
        if _within_grant(request, grant):
            in_scope = grant
            break

    if in_scope is None:
        return PermissionDecision(
            decision=Decision.CONFIRM,
            risk=RiskLevel.HIGH,
            reason="超出已有授权的资源范围",
            matched_rule="out_of_scope",
        )

    if risk is RiskLevel.HIGH:
        return PermissionDecision(
            decision=Decision.CONFIRM,
            risk=RiskLevel.HIGH,
            reason=f"高影响操作需二次确认：{risk_reason}",
            matched_rule=f"high_impact:{in_scope.id}",
        )

    return PermissionDecision(
        decision=Decision.ALLOW,
        risk=RiskLevel.NORMAL,
        reason=f"在授权范围内（{in_scope.id}）",
        matched_rule=in_scope.id,
    )


def session_scope_for(request: ToolRequest, workspace_root: str) -> GrantScope | None:
    """为一次需确认的请求给出「本会话允许」的授权边界。纯函数。

    - 文件读写：整个工作区（文件工具本就被路径守卫限制在工作区内）；
    - 联网：本次请求的主机；
    - 终端：不限命令（§11.2 的破坏性命令仍按高影响逐次确认）。

    建立这条授权后本次请求**仍需确认**时返回 ``None``——高影响操作、没有可授权
    的目标（如网页搜索）都属此类：给用户一个「记住」按钮却照样弹框，比不给更糟。
    是否覆盖交给 :func:`evaluate` 本身判断，不另写一套规则。
    """
    if request.capability in (Capability.FILE_READ, Capability.FILE_WRITE):
        scope = GrantScope(request.capability, allowed_paths=(workspace_root,))
    elif request.capability is Capability.NETWORK:
        domains = tuple(d for d in request.scope.domains if d)
        scope = GrantScope(request.capability, allowed_domains=domains)
    else:
        scope = GrantScope(request.capability)
    probe = PermissionGrant(
        id="proposed",
        conversation_id="",
        capability=scope.capability,
        created_at=datetime.fromtimestamp(0, UTC),
        allowed_paths=scope.allowed_paths,
        allowed_domains=scope.allowed_domains,
    )
    if evaluate(request, [probe]).decision is not Decision.ALLOW:
        return None
    return scope


def _within_grant(request: ToolRequest, grant: PermissionGrant) -> bool:
    """请求的资源是否落在该授权内。不同能力看不同维度。"""
    if request.capability in (Capability.FILE_READ, Capability.FILE_WRITE):
        if not request.scope.paths:
            return False
        return all(
            any(path_is_within(p, allowed) for allowed in grant.allowed_paths)
            for p in request.scope.paths
        )
    if request.capability is Capability.NETWORK:
        if "*" in grant.allowed_domains:
            return True
        if not request.scope.domains:
            return False
        return all(domain_is_allowed(d, grant.allowed_domains) for d in request.scope.domains)
    if request.capability is Capability.TERMINAL:
        cwd = request.scope.working_directory
        if cwd is None or not grant.allowed_working_directories:
            return True
        return any(path_is_within(cwd, d) for d in grant.allowed_working_directories)
    return False
