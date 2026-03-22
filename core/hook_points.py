"""Stable hook point names for :class:`core.hooks.AgentHooks`.

Register handlers with :meth:`AgentHooks.register` using these constants.
Handlers receive a mutable ``context`` dict; sync handlers may return a new
dict to replace it. Async handlers are supported at ``await`` boundaries via
:meth:`AgentHooks.run_async`.
"""

from __future__ import annotations

SESSION_START = "session.start"
SESSION_END = "session.end"

ROUND_START = "round.start"
ROUND_END = "round.end"

LLM_BEFORE = "llm.before"
LLM_AFTER = "llm.after"

TOOLS_BATCH_BEFORE = "tools.batch.before"
TOOLS_BATCH_AFTER = "tools.batch.after"

TOOL_BEFORE = "tool.before"
TOOL_AFTER = "tool.after"
