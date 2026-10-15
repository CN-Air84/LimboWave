"""Reversible runtime overlays: retain the raw tree, compact only the model context."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from limbowave.domain.compaction import CompressionVersion
from limbowave.domain.conversation import Message
from limbowave.domain.runtime_mirror import RuntimeEntryMetadata
from limbowave.domain.runtime_state import RuntimeStateSnapshot, fingerprint_from_entry


def without_application_compactions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove our overlays, reconnecting descendants to the original immutable history."""
    removed = {
        str(e["id"]): e.get("parentId")
        for e in entries
        if e.get("type") == "compaction"
        and isinstance(e.get("details"), dict)
        and e["details"].get("limbowaveVersion")
    }
    if not removed:
        # 普通未压缩会话只需复制列表；不为每条消息分配遍历集合或重建父链。
        return list(entries)
    result = []
    changed_context = set(removed)
    # 多个后继可以引用同一覆盖层链；只在本次投影内缓存跳过覆盖层后的父节点。
    resolved_parents: dict[str, str | None] = {}
    for entry in entries:
        if str(entry.get("id")) in removed:
            continue
        parent = entry.get("parentId")
        if parent in changed_context:
            changed_context.add(str(entry.get("id")))
            message = entry.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                # Restoring originals invalidates usage measured on a compressed prompt.
                # Change the runtime copy only; immutable billing/audit records stay intact.
                usage = dict(message.get("usage") or {})
                usage.update(
                    dict.fromkeys(("input", "output", "cacheRead", "cacheWrite", "totalTokens"), 0)
                )
                entry = {**entry, "message": {**message, "usage": usage}}
        visited: set[str] = set()
        while parent in removed:
            if parent in resolved_parents:
                parent = resolved_parents[parent]
                break
            if parent in visited:
                raise ValueError("压缩覆盖层父链存在循环")
            visited.add(parent)
            parent = removed[parent]
        resolved_parents.update(dict.fromkeys(visited, parent))
        result.append({**entry, "parentId": parent} if parent != entry.get("parentId") else entry)
    return result


def uncompressed_snapshot(snapshot: RuntimeStateSnapshot) -> RuntimeStateSnapshot:
    """Mirrors are immutable; discard any old branch's overlays in the runtime copy."""
    entries = without_application_compactions(snapshot.entries)
    unchanged = len(entries) == len(snapshot.entries) and all(
        current is original for current, original in zip(entries, snapshot.entries, strict=True)
    )
    return replace(
        snapshot, entries=entries, leaf_entry_id=entries[-1]["id"] if entries else None,
        fingerprint=(snapshot.fingerprint if unchanged else [
            fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(entries)
        ]),
    )


def project_compression(
    snapshot: RuntimeStateSnapshot,
    version: CompressionVersion,
    messages: list[Message],
    mirrors: Sequence[RuntimeEntryMetadata],
) -> RuntimeStateSnapshot:
    """Append a native compaction with the preview boundary; retain subsequent turns."""
    ids = tuple(m.id for m in messages)
    covered = version.input_message_ids
    if not covered or ids[: len(covered)] != covered:
        raise ValueError("压缩版本的消息范围已变化，请重新生成预览")
    summary = version.effective_summary.strip()
    if not summary:
        raise ValueError("压缩摘要为空，不能启用")
    last = messages[len(covered) - 1]
    boundary_ids = {
        m.entry_id
        for m in mirrors
        if (last.run_id is not None and m.run_id == last.run_id) or m.message_id == last.id
    }
    entries = without_application_compactions(snapshot.entries)
    positions = [i for i, entry in enumerate(entries) if entry.get("id") in boundary_ids]
    if not positions:
        raise ValueError("压缩范围缺少完整运行时记录，未修改上下文")
    boundary = max(positions)
    # An edited/generated summary is not trusted to retain whitelist text verbatim.
    whitelist = [
        m.content
        for m in messages[: len(covered)]
        if (m.id in version.whitelist_message_ids or m.is_whitelisted)
        and m.content
        and m.content not in summary
    ]
    if whitelist:
        summary += "\n\n## 白名单原文\n" + "\n\n".join(whitelist)
    # Mirrors deduplicate by entry id. Reusing a marker with a different parent would
    # reconnect future messages to the old leaf and silently drop intervening turns.
    identity = summary + "\0" + str(entries[-1]["id"])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    marker_id = f"limbowave-{version.id}-{digest}"
    marker = {
        "id": marker_id,
        "type": "compaction",
        "parentId": entries[-1]["id"],
        "timestamp": version.created_at.isoformat(),
        "summary": summary,
        "firstKeptEntryId": (
            entries[boundary + 1]["id"] if boundary + 1 < len(entries) else marker_id
        ),
        "tokensBefore": version.tokens_before,
        "details": {"limbowaveVersion": version.id},
    }
    # Native compaction is appended at the leaf. Its firstKeptEntryId retains the
    # post-preview tail, while making all pre-compaction usage explicitly unknown.
    # Keeping original parent links also preserves fork/rollback semantics.
    projected = [*entries, marker]
    return replace(
        snapshot,
        entries=projected,
        leaf_entry_id=projected[-1]["id"],
        fingerprint=[fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(projected)],
    )
