"""White-list transport projections, never serialize application objects wholesale."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

FIELDS = {
    "state": {
        "server_epoch",
        "revision",
        "available",
        "busy",
        "conversation_id",
        "branch_id",
        "run_id",
        "seq",
        "stream",
    },
    "model": {"id", "name"},
    "branch": {"id", "title"},
    "conversation": {
        "branches",
        "id",
        "conversation_id",
        "branch_id",
        "title",
        "created_at",
        "updated_at",
        "revision",
    },
    "message": {
        "tools",
        "id",
        "message_id",
        "conversation_id",
        "branch_id",
        "role",
        "text",
        "content",
        "thinking",
        "created_at",
        "run_id",
        "status",
    },
    "receipt": {
        "server_epoch",
        "client_command_id",
        "status",
        "revision",
        "run_id",
        "conversation_id",
        "branch_id",
        "error",
    },
    "event": {
        "protocol_version",
        "server_epoch",
        "seq",
        "kind",
        "conversation_id",
        "branch_id",
        "run_id",
        "message_id",
        "payload",
    },
    "segment": {"content", "thinking"},
    "tool": {"tool_id", "name", "status"},
    "payload": {
        "tools",
        "segments",
        "attempt",
        "max_attempts",
        "delay_ms",
        "text",
        "delta",
        "thinking",
        "status",
        "code",
        "reason",
        "revision",
        "title",
        "role",
        "tool_name",
        "name",
        "phase",
        "is_error",
        "stop_reason",
        "tool_call_id",
        "message_id",
        "run_id",
        "conversation_id",
        "branch_id",
        "available",
        "busy",
        "content",
        "seq",
        "server_epoch",
    },
}


def project(value: Any, kind: str) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in FIELDS[kind]:
        if key not in value:
            continue
        item = value[key]
        if key in ("stream", "payload"):
            result[key] = None if item is None else project(item, "payload")
        elif key == "tools" and isinstance(item, list):
            result[key] = [project(tool, "tool") for tool in item]
        elif key == "segments" and isinstance(item, list):
            result[key] = [project(segment, "segment") for segment in item[:100]]
        elif key == "branches" and isinstance(item, list):
            result[key] = [project(branch, "branch") for branch in item[:100]]
        elif key == "error":
            result[key] = None if item is None else safe_error(item)
        elif item is None or isinstance(item, (str, bool, int, float)):
            result[key] = item
    return result


def page(value: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "items": [project(item, kind) for item in value.get("items", [])[:100]],
        "next_cursor": value.get("next_cursor"),
    }


SAFE_CODES = frozenset(
    {
        "runtime_unavailable",
        "epoch_mismatch",
        "run_mismatch",
        "runtime_busy",
        "revision_mismatch",
        "target_restore_failed",
        "send_rejected",
        "command_failed",
        "device_revoked",
        "invalid_command",
        "invalid_revision",
        "invalid_model",
        "model_unavailable",
        "target_not_found",
        "invalid_command_id",
        "command_too_large",
        "command_id_reused",
        "receipt_capacity",
        "invalid_cursor",
        "branch_not_found",
    }
)


def safe_error(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    code = value.get("code")
    status = value.get("status")
    return {
        "code": code if code in SAFE_CODES else "command_failed",
        "status": status if status in (400, 401, 403, 404, 409, 413, 429, 503) else 500,
    }
