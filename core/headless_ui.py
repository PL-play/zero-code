from __future__ import annotations

import sys
from typing import Optional

from core.types import AgentMessage
from core.ui_adapter import UIAdapter


class HeadlessUI(UIAdapter):
    """Minimal UI adapter that prints to stdout.

    This allows running the agent core without Textual, which is useful
    for tests, batch runs, or alternative frontends that only need a
    plain-text interface.
    """

    def __init__(self) -> None:
        self._last_status: str = ""

    def show_message(self, message: AgentMessage, *, elapsed: Optional[float] = None) -> None:
        prefix = message.role.upper()
        suffix = f" ({elapsed:.2f}s)" if elapsed is not None else ""
        text = f"[{prefix}]{suffix} {message.content}"
        print(text)

    def update_status(self, text: str) -> None:
        self._last_status = text
        print(f"[STATUS] {text}", file=sys.stderr)

    def log_agent(self, text: str) -> None:
        print(f"[AGENT] {text}", file=sys.stderr)

    def update_usage(self, usage_summary: str) -> None:
        print(f"[USAGE] {usage_summary}", file=sys.stderr)

    def show_tool_call_brief(self, name: str, brief: str) -> None:
        print(f"[TOOL] {brief}", file=sys.stderr)

    def show_tool_call_detail(self, name: str, output: str, tool_input: dict | None = None) -> None:
        print(f"[TOOL:{name}] input={tool_input or {}}", file=sys.stderr)
        print(output, file=sys.stderr)

    def handle_stream_delta(self, stream_id: str, text: str, *, is_think: bool) -> None:
        # Simple streaming to stdout/stderr.
        target = sys.stderr if is_think else sys.stdout
        print(text, end="", file=target, flush=True)

