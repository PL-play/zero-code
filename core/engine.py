from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional

from core.agent import agent_loop
from core.events import AgentEventBus, DEFAULT_EVENT_BUS
from core.hooks import AgentHooks, DEFAULT_HOOKS
from core.middlewares import AgentMiddleware


@dataclass(frozen=True)
class AgentRunResult:
    session_id: str
    text: str


class AgentRunner:
    """Primary library entrypoint for running the agent core.

    The runner allows injecting:
    - an event bus (pub/sub)
    - hooks (extensible behavior points; currently optional)
    - middlewares (caching/replay/analytics via event subscriptions)

    UI is not required: consumers can subscribe to the event bus themselves.
    """

    def __init__(
        self,
        *,
        event_bus: AgentEventBus | None = None,
        hooks: AgentHooks | None = None,
        middlewares: Optional[List[AgentMiddleware]] = None,
    ) -> None:
        self.event_bus = event_bus or DEFAULT_EVENT_BUS
        self.hooks = hooks or DEFAULT_HOOKS
        self.middlewares = middlewares or []

        for mw in self.middlewares:
            mw.register(self.event_bus)

    def run(
        self,
        messages: list,
        stop_event: threading.Event | None = None,
        *,
        session_id: str | None = None,
    ) -> AgentRunResult:
        sid = session_id or uuid.uuid4().hex
        text = agent_loop(
            messages,
            stop_event,
            event_bus=self.event_bus,
            session_id=sid,
            hooks=self.hooks,
        )
        return AgentRunResult(session_id=sid, text=text)

