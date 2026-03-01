"""Tool registry placeholder for future steps."""

from typing import Any, Callable

TOOLS: list[dict[str, Any]] = []
TOOL_HANDLERS: dict[str, Callable[..., str]] = {}


def dispatch_tool_call(tool_name: str, **kwargs: Any) -> str:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"
    try:
        return handler(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive guard
        return f"Tool error ({tool_name}): {exc}"
