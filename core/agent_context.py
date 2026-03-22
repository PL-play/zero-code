"""Per-run context for agent core (event bus + hooks).

Used by :class:`core.runner.AgentRunner` so library callers can inject an
isolated bus/hooks without touching global defaults.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.events import AgentEventBus
    from core.hooks import AgentHooks

from core.events import DEFAULT_EVENT_BUS, AgentEventBus
from core.hooks import DEFAULT_HOOKS, AgentHooks

_current_event_bus: ContextVar[AgentEventBus | None] = ContextVar(
    "zero_code_event_bus", default=None
)
_current_hooks: ContextVar[AgentHooks | None] = ContextVar(
    "zero_code_hooks", default=None
)


def get_event_bus() -> AgentEventBus:
    bus = _current_event_bus.get()
    return DEFAULT_EVENT_BUS if bus is None else bus


def get_hooks() -> AgentHooks:
    h = _current_hooks.get()
    return DEFAULT_HOOKS if h is None else h


def set_event_bus(bus: AgentEventBus | None) -> Token:
    return _current_event_bus.set(bus)


def set_hooks(hooks: AgentHooks | None) -> Token:
    return _current_hooks.set(hooks)


def reset_event_bus(token: Token) -> None:
    _current_event_bus.reset(token)


def reset_hooks(token: Token) -> None:
    _current_hooks.reset(token)
