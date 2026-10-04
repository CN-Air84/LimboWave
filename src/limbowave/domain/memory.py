"""记忆设置与加密文档；每轮快照保留分叉所需的历史版本。"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MemoryPolicy(StrEnum):
    INHERIT = "inherit"
    ASK = "ask"
    ALLOW = "allow"


class MemorySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    global_interval: int = Field(default=15, ge=1, le=30, strict=True)
    session_interval: int = Field(default=15, ge=1, le=30, strict=True)
    default_policy: str = Field(default="ask", pattern=r"^(ask|allow)$")


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    id: str
    payload: str
    conversation_id: str | None = None
    branch_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    content: str
    source: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryRunContext:
    run_id: str
    conversation_id: str
    branch_id: str
    user_message_id: str
