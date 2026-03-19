from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from core.types import AgentEvent, AgentEventType


EventHandler = Callable[[AgentEvent], Any] | Callable[[AgentEvent], Awaitable[Any]]


@dataclass
class _SubscribedHandler:
    handler: EventHandler
    priority: int
    order: int


class AgentEventBus:
    """Simple in-process pub/sub bus for agent events.

    - Handlers are ordered by (priority, registration order).
    - Supports both sync and async handlers.
    """

    def __init__(self) -> None:
        self._handlers: Dict[AgentEventType, List[_SubscribedHandler]] = defaultdict(list)
        self._global_handlers: List[_SubscribedHandler] = []
        self._counter = 0

    def subscribe(
        self,
        event_type: AgentEventType | None,
        handler: EventHandler,
        *,
        priority: int = 0,
    ) -> None:
        """Subscribe to a specific event type, or all events when event_type is None."""
        self._counter += 1
        wrapped = _SubscribedHandler(handler=handler, priority=priority, order=self._counter)
        if event_type is None:
            self._global_handlers.append(wrapped)
            self._global_handlers.sort(key=lambda h: (h.priority, h.order))
        else:
            self._handlers[event_type].append(wrapped)
            self._handlers[event_type].sort(key=lambda h: (h.priority, h.order))

    def unsubscribe(self, handler: EventHandler) -> None:
        """Remove a handler from all registrations."""
        self._global_handlers = [h for h in self._global_handlers if h.handler is not handler]
        for etype, handlers in list(self._handlers.items()):
            self._handlers[etype] = [h for h in handlers if h.handler is not handler]
            if not self._handlers[etype]:
                self._handlers.pop(etype, None)

    def publish(self, event: AgentEvent) -> None:
        """Publish an event synchronously.

        Async handlers are scheduled as tasks on the current loop when present.
        """
        # Collect handlers for this event
        handlers: List[_SubscribedHandler] = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.type, []))

        loop: asyncio.AbstractEventLoop | None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for sub in handlers:
            result = sub.handler(event)
            if asyncio.iscoroutine(result):
                if loop is not None:
                    loop.create_task(result)  # fire-and-forget
                else:
                    asyncio.run(result)

    async def publish_async(self, event: AgentEvent) -> None:
        """Async variant of publish that awaits async handlers."""
        handlers: List[_SubscribedHandler] = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.type, []))

        for sub in handlers:
            result = sub.handler(event)
            if asyncio.iscoroutine(result):
                await result


# Global default bus instance used by agent core & UI adapters.
DEFAULT_EVENT_BUS = AgentEventBus()

