from __future__ import annotations

from typing import Protocol, Optional

from core.types import AgentMessage


class UIAdapter(Protocol):
    """Abstract UI interface used by the agent core.

    Current Textual TUI implements this interface; future UIs (web/IDE)
    can implement their own adapters without touching the agent loop.
    """

    def show_message(self, message: AgentMessage, *, elapsed: Optional[float] = None) -> None:
        ...

    def update_status(self, text: str) -> None:
        ...

    def log_agent(self, text: str) -> None:
        ...

    def update_usage(self, usage_summary: str) -> None:
        ...

    def show_tool_call_brief(self, name: str, brief: str) -> None:
        ...

    def show_tool_call_detail(self, name: str, output: str, tool_input: dict | None = None) -> None:
        ...

    def handle_stream_delta(self, stream_id: str, text: str, *, is_think: bool) -> None:
        ...

