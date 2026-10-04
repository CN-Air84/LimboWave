"""权限网关服务（Phase 6 / 设计计划 §十一）。

把领域层的纯规则（:mod:`limbowave.domain.permissions`）与存储、审计接起来：

- 授权按会话持久（§11.1），重开会话自动恢复——授权在库里，不在内存；
- 每次决策写一条审计（§11.3）：工具、参数、匹配规则、结果、是否用户确认、实际范围；
- 高影响操作的二次确认由**调用方**（UI）执行；本服务负责判定「是否需要确认」
  并如实记录用户的选择。

判定本身是纯函数（``evaluate``），所以「为什么放行/拒绝」可测、可审计。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.permissions import (
    Capability,
    Decision,
    ExecutionMode,
    PermissionAudit,
    PermissionDecision,
    PermissionGrant,
    PermissionPreset,
    ToolRequest,
    evaluate,
)


class PermissionService:
    """会话级授权 + 判定 + 审计。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    # ---------- 授权管理（§11.1） ----------

    def grant(
        self,
        conversation_id: str,
        capability: Capability,
        *,
        allowed_paths: tuple[str, ...] = (),
        allowed_domains: tuple[str, ...] = (),
        allowed_working_directories: tuple[str, ...] = (),
        command_classes: tuple[str, ...] = (),
        note: str = "",
    ) -> PermissionGrant:
        """授予一项会话级权限。重开会话时自动恢复（授权在库里）。"""
        record = PermissionGrant(
            id=f"grant_{uuid4().hex[:16]}",
            conversation_id=conversation_id,
            capability=capability,
            created_at=datetime.now(UTC),
            allowed_paths=allowed_paths,
            allowed_domains=allowed_domains,
            allowed_working_directories=allowed_working_directories,
            command_classes=command_classes,
            note=note,
        )
        with self._uow_factory() as uow:
            uow.permissions.add_grant(record)
            uow.commit()
        return record

    def revoke(self, grant_id: str) -> bool:
        """撤销授权（§11.1：用户可随时撤销）。"""
        with self._uow_factory() as uow:
            if uow.permissions.get_grant(grant_id) is None:
                return False
            uow.permissions.delete_grant(grant_id)
            uow.commit()
            return True

    def list_grants(self, conversation_id: str) -> list[PermissionGrant]:
        with self._uow_factory() as uow:
            return uow.permissions.list_grants(conversation_id)

    def get_preset(self, conversation_id: str) -> PermissionPreset:
        """读取会话权限档位；会话不存在时回落为默认档「自由读取」。"""
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
        return (
            conversation.permission_preset
            if conversation is not None
            else PermissionPreset.READ_ONLY
        )

    def set_preset(self, conversation_id: str, preset: PermissionPreset) -> bool:
        """修改会话权限档位。档位与细粒度授权分开保存。"""
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                return False
            uow.conversations.update(replace(conversation, permission_preset=preset))
            uow.commit()
        return True

    def replace_custom_grants(
        self,
        conversation_id: str,
        capabilities: set[Capability],
        *,
        workspace_root: str,
    ) -> None:
        """用自定义面板的四个能力开关替换当前会话授权。"""
        for grant in self.list_grants(conversation_id):
            self.revoke(grant.id)
        for capability in Capability:
            if capability not in capabilities:
                continue
            if capability in (Capability.FILE_READ, Capability.FILE_WRITE):
                self.grant(
                    conversation_id,
                    capability,
                    allowed_paths=(workspace_root,),
                    note="输入区权限：自定义",
                )
            elif capability is Capability.NETWORK:
                self.grant(
                    conversation_id,
                    capability,
                    allowed_domains=("*",),
                    note="输入区权限：自定义",
                )
            else:
                self.grant(
                    conversation_id,
                    capability,
                    note="输入区权限：自定义",
                )

    # ---------- 判定（§11.2） ----------

    def authorize(
        self,
        request: ToolRequest,
        conversation_id: str,
        *,
        mode: ExecutionMode = ExecutionMode.BUILTIN_TOOLS,
        user_confirmed: bool = False,
    ) -> PermissionDecision:
        """对一个工具请求给出判定，并写审计。

        ``user_confirmed``：调用方在需要确认时已拿到用户答复（True=同意）。
        判定为 CONFIRM 且 ``user_confirmed=True`` 时，实际结果按 ALLOW 记录并执行；
        判定为 ALLOW 时直接执行。审计如实记录「是否用户确认」。
        """
        with self._uow_factory() as uow:
            grants = uow.permissions.list_grants(conversation_id)

        decision = evaluate(request, grants, mode=mode)
        recorded = decision.decision
        if recorded is Decision.CONFIRM and user_confirmed:
            recorded = Decision.ALLOW

        audit = PermissionAudit(
            id=f"audit_{uuid4().hex[:16]}",
            conversation_id=conversation_id,
            created_at=datetime.now(UTC),
            tool_name=request.tool_name,
            capability=request.capability,
            matched_rule=decision.matched_rule,
            decision=recorded,
            risk=decision.risk,
            user_confirmed=user_confirmed,
            params=request.raw_params,
            actual_paths=request.scope.paths,
        )
        with self._uow_factory() as uow:
            uow.permissions.add_audit(audit)
            uow.commit()
        return decision

    def is_allowed(
        self,
        request: ToolRequest,
        conversation_id: str,
        *,
        mode: ExecutionMode = ExecutionMode.BUILTIN_TOOLS,
        user_confirmed: bool = False,
    ) -> bool:
        """判定并返回「是否可执行」。CONFIRM 且用户已同意 → True。"""
        decision = self.authorize(
            request, conversation_id, mode=mode, user_confirmed=user_confirmed
        )
        if decision.decision is Decision.ALLOW:
            return True
        return decision.decision is Decision.CONFIRM and user_confirmed

    # ---------- 审计（§11.3） ----------

    def list_audit(self, conversation_id: str) -> list[PermissionAudit]:
        with self._uow_factory() as uow:
            return uow.permissions.list_audit(conversation_id)
