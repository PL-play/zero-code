from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Literal, Optional


Role = Literal["user", "assistant", "system", "tool"]


@dataclass
class ToolCall:
    """Structured representation of a tool call in the agent conversation."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentMessage:
    """Unified message format between agent core and UIs."""

    role: Role
    content: str
    tool_calls: Optional[List[ToolCall]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentEventType(str, Enum):
    """High-level lifecycle and streaming events emitted by the agent core."""

    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"

    ROUND_STARTED = "round_started"
    ROUND_COMPLETED = "round_completed"

    STREAM_STARTED = "stream_started"
    STREAM_DELTA = "stream_delta"
    STREAM_COMPLETED = "stream_completed"
    STREAM_THINK_DELTA = "stream_think_delta"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_ERROR = "tool_call_error"

    USAGE_UPDATED = "usage_updated"
    STATUS_CHANGED = "status_changed"

    ROUND_TOOLS_PRESENT = "round_tools_present"

    TODO_UPDATED = "todo_updated"

    # Debug/log channel (for internal inspection panels).
    SYSTEM_LOG = "system_log"

    AGENT_ERROR = "agent_error"
    USER_NOTIFICATION = "user_notification"

    # Sub-agent specific UI events.
    SUBAGENT_TASK_START = "subagent_task_start"
    SUBAGENT_TEXT = "subagent_text"
    SUBAGENT_LIMIT = "subagent_limit"


@dataclass
class AgentEvent:
    """Event envelope flowing through the AgentEventBus."""

    type: AgentEventType
    payload: Dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    round_id: Optional[int] = None
    timestamp: float | None = None

