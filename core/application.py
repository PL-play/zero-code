"""Application shell: :class:`Config` + :class:`AgentLoop`.

Entry shape (UI 由独立模块通过 ``on_start`` / ``subscribe`` 接入，本模块无 UI 预设)::

    config = Config()
    # optional: your_frontend.install(config)
    agent = AgentLoop(config)
    agent.run()

The agent core only talks to :attr:`Config.event_bus` (via context set by
:class:`core.runner.AgentRunner`). Frontends subscribe on the same bus and may
register :meth:`Config.on_app_start` hooks to run a blocking main loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List

from core.events import AgentEventBus
from core.hooks import AgentHooks
from core.runner import AgentRunner, EventMiddleware
from core.types import AgentEventType

#: Receives the active :class:`Config` (same instance passed to :class:`AgentLoop`).
LifecycleHook = Callable[["Config"], None]


@dataclass
class Config:
    """Runtime wiring: shared event bus, hooks, middlewares, and startup hooks.

    Use :meth:`subscribe` / :meth:`add_middleware` / :meth:`on_app_start` for fluent
    configuration. ``on_start`` runs in order when :meth:`AgentLoop.run` is called
    (e.g. a blocking UI main loop).
    """

    event_bus: AgentEventBus = field(
        default_factory=lambda: AgentEventBus(suppress_subscriber_errors=True)
    )
    hooks: AgentHooks = field(default_factory=AgentHooks)
    middlewares: List[EventMiddleware] = field(default_factory=list)
    on_start: List[LifecycleHook] = field(default_factory=list)

    def subscribe(
        self,
        event_type: AgentEventType | None,
        handler: Callable,
        *,
        priority: int = 0,
    ) -> Config:
        """Register a subscriber on this config's bus (chainable)."""
        self.event_bus.subscribe(event_type, handler, priority=priority)
        return self

    def add_middleware(self, mw: EventMiddleware) -> Config:
        self.middlewares.append(mw)
        return self

    def on_app_start(self, fn: LifecycleHook) -> Config:
        """Append a hook invoked from :meth:`AgentLoop.run` (chainable)."""
        self.on_start.append(fn)
        return self

    def register_hook(
        self, hook_point: str, handler: Callable, *, order: int = 0
    ) -> Config:
        """Register on :class:`core.hooks.AgentHooks` (chainable)."""
        self.hooks.register(hook_point, handler, order=order)
        return self

    def build_runner(self) -> AgentRunner:
        """Create an :class:`AgentRunner` bound to this configuration."""
        return AgentRunner(
            event_bus=self.event_bus,
            hooks=self.hooks,
            middlewares=self.middlewares,
        )


class AgentLoop:
    """High-level façade: owns a :class:`Config` and its :class:`AgentRunner`."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config if config is not None else Config()
        self._runner = self.config.build_runner()

    @property
    def runner(self) -> AgentRunner:
        return self._runner

    def run(self) -> None:
        """Run lifecycle hooks (typically one blocking UI main loop)."""
        for hook in self.config.on_start:
            hook(self.config)
