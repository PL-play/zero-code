from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Tuple


HookHandler = Callable[[Dict[str, Any]], Any] | Callable[[Dict[str, Any]], Awaitable[Any]]


class AgentHooks:
    """Lightweight hook manager for semantic hook points in the agent loop.

    - Each hook_point can register multiple handlers with an integer order.
    - Handlers receive and may mutate a shared `context` dict.
    - Supports both sync and async handlers.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Tuple[int, HookHandler]]] = {}

    def register(self, hook_point: str, handler: HookHandler, *, order: int = 0) -> None:
        handlers = self._handlers.setdefault(hook_point, [])
        handlers.append((order, handler))
        handlers.sort(key=lambda item: item[0])

    def clear(self, hook_point: str | None = None) -> None:
        if hook_point is None:
            self._handlers.clear()
        else:
            self._handlers.pop(hook_point, None)

    def run(self, hook_point: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Run all handlers for a hook point synchronously."""
        handlers = self._handlers.get(hook_point, [])
        for _, handler in handlers:
            result = handler(context)
            # Allow handler to return a new context dict
            if isinstance(result, dict):
                context = result
        return context

    async def run_async(self, hook_point: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Async variant that awaits async handlers."""
        handlers = self._handlers.get(hook_point, [])
        for _, handler in handlers:
            result = handler(context)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            if isinstance(result, dict):
                context = result
        return context


# Global default hooks instance used by agent core.
DEFAULT_HOOKS = AgentHooks()

