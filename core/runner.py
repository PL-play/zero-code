"""AgentRunner / AgentEngine: inject event bus, hooks, and optional middleware.

Typical usage (isolated bus for tests or alternate UIs)::

    runner = AgentRunner(event_bus=AgentEventBus())
    with runner.session():
        reply = agent_loop(messages)

Async (same asyncio task / copied context in ``asyncio.to_thread``)::

    async with runner.session_async():
        reply = await agent_loop_async(messages)

Middleware: each callable takes an :class:`~core.types.AgentEvent` and returns
either the same event, a modified event, or ``None`` to drop the event.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Callable, Iterator, List, Sequence

from core.agent_context import (
    get_event_bus,
    reset_event_bus,
    reset_hooks,
    set_event_bus,
    set_hooks,
)
from core.events import DEFAULT_EVENT_BUS, AgentEventBus
from core.hooks import DEFAULT_HOOKS, AgentHooks
from core.types import AgentEvent


EventMiddleware = Callable[[AgentEvent], AgentEvent | None]


class _MiddlewareEventBus:
    """Delegates subscribe/unsubscribe; runs middleware chain on publish."""

    __slots__ = ("_inner", "_middlewares")

    def __init__(
        self,
        inner: AgentEventBus,
        middlewares: Sequence[EventMiddleware],
    ) -> None:
        self._inner = inner
        self._middlewares = list(middlewares)

    def subscribe(self, *args, **kwargs):
        return self._inner.subscribe(*args, **kwargs)

    def unsubscribe(self, *args, **kwargs):
        return self._inner.unsubscribe(*args, **kwargs)

    def publish(self, event: AgentEvent) -> None:
        e: AgentEvent | None = event
        for m in self._middlewares:
            if e is None:
                return
            e = m(e)
        if e is not None:
            self._inner.publish(e)

    async def publish_async(self, event: AgentEvent) -> None:
        e: AgentEvent | None = event
        for m in self._middlewares:
            if e is None:
                return
            e = m(e)
        if e is not None:
            await self._inner.publish_async(e)


class AgentRunner:
    """Holds configuration for one logical agent runtime and activates it in a context scope."""

    def __init__(
        self,
        *,
        event_bus: AgentEventBus | None = None,
        hooks: AgentHooks | None = None,
        middlewares: Sequence[EventMiddleware] | None = None,
    ) -> None:
        base = event_bus if event_bus is not None else DEFAULT_EVENT_BUS
        mw: List[EventMiddleware] = list(middlewares or [])
        self.event_bus: AgentEventBus = (
            _MiddlewareEventBus(base, mw) if mw else base  # type: ignore[assignment]
        )
        self.hooks: AgentHooks = hooks if hooks is not None else DEFAULT_HOOKS

    @contextmanager
    def session(self) -> Iterator["AgentRunner"]:
        tok_bus = set_event_bus(self.event_bus)
        tok_hooks = set_hooks(self.hooks)
        try:
            yield self
        finally:
            reset_hooks(tok_hooks)
            reset_event_bus(tok_bus)

    @asynccontextmanager
    async def session_async(self):
        tok_bus = set_event_bus(self.event_bus)
        tok_hooks = set_hooks(self.hooks)
        try:
            yield self
        finally:
            reset_hooks(tok_hooks)
            reset_event_bus(tok_bus)

    def run(self, messages: list, stop_event=None) -> str:
        """Sync entry: runs :func:`core.agent.agent_loop` under this runner's context."""
        from core.agent import agent_loop

        with self.session(self):
            return agent_loop(messages, stop_event=stop_event)

    async def run_async(self, messages: list, stop_event=None) -> str:
        """Async entry: await :func:`core.agent.agent_loop_async` under context."""
        from core.agent import agent_loop_async

        async with self.session_async(self):
            return await agent_loop_async(messages, stop_event=stop_event)


def default_runner() -> AgentRunner:
    """Runner that uses the process-wide default bus and hooks (explicit but no isolation)."""
    return AgentRunner(event_bus=get_event_bus(), hooks=DEFAULT_HOOKS)
