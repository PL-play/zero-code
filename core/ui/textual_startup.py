"""Textual UI entry hook for :class:`core.application.Config`.

Keeps ``ZeroCodeApp`` construction and :func:`UI.set_app` out of ``zero_code.py``;
swap this hook for another frontend without changing the agent core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application import Config


def start_textual_app(config: Config) -> None:
    """Mount the Textual app on ``config.event_bus`` and block until exit."""
    from core.state import UI
    from core.tui import ZeroCodeApp

    app = ZeroCodeApp(event_bus=config.event_bus)
    UI.set_app(app)
    app.run()
