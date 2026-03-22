from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional

from core.events import AgentEventBus
from core.types import AgentEvent


class AgentMiddleware:
    """Base class for agent middlewares.

    Middlewares can subscribe to the event bus to implement caching, replay,
    analytics, or stream aggregation.
    """

    def register(self, bus: AgentEventBus) -> None:  # pragma: no cover
        raise NotImplementedError


class InMemoryEventStoreMiddleware(AgentMiddleware):
    """Keep recent events in memory for debugging/replay."""

    def __init__(self, max_events: int = 50000) -> None:
        self._events: Deque[AgentEvent] = deque(maxlen=max_events)

    def register(self, bus: AgentEventBus) -> None:
        bus.subscribe(None, self._on_event, priority=-1000)

    def _on_event(self, event: AgentEvent) -> None:
        self._events.append(event)

    def all_events(self) -> List[AgentEvent]:
        return list(self._events)

    def events_by_session(self, session_id: str) -> List[AgentEvent]:
        return [e for e in self._events if e.session_id == session_id]

