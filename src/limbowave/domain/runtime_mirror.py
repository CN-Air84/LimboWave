"""Pi 运行时条目的应用侧镜像。

**Pi entry ID 不充当应用主键。** 镜像有自己的 ``id``；``entry_id`` 只是对 Pi 侧条目的引用，
``message_id`` 在能对上时把镜像关联回应用消息。

存在意义（Phase 1C 的地基）：Runtime 崩溃后，应用要能凭自己的数据重建 Pi 的上下文。
本阶段只负责把镜像**正确、完整**地存下来，不负责恢复协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeEntryMirror:
    id: str
    conversation_id: str
    entry_id: str
    entry_type: str
    captured_at: datetime
    run_id: str | None = None
    parent_entry_id: str | None = None
    message_id: str | None = None
    runtime_instance_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
