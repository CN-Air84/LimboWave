"""Server-only tool policy: no host tools are registered in the first release."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerToolPolicy:
    """Explicit allow-list used by the headless composition root."""

    allow_chat: bool = True
    allow_memory_write: bool = True
    allow_host_tools: bool = False

    def allows(self, tool_name: str) -> bool:
        if tool_name == "add_session_memory":
            return self.allow_memory_write
        return False if not self.allow_host_tools else tool_name in {"get_current_datetime"}

    def validate(self, tool_name: str) -> None:
        if not self.allows(tool_name):
            raise PermissionError("server_tool_denied")
