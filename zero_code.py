#!/usr/bin/env python3
"""
Thin entrypoint for zero-code.

Only keeps:
- main() entry
- minimal runtime config exports
"""

from core.agent import agent_loop
from core.commands import COMMAND_DISPATCH, SLASH_COMMANDS, _handle_help
from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKDIR, client
from core.state import UI
from core.tui import ZeroCodeApp

__all__ = [
    "WORKDIR",
    "AGENT_DIR",
    "SKILLS_DIR",
    "MODEL",
    "client",
    "main",
]


def main():
    app = ZeroCodeApp()
    UI.set_app(app)
    app.run()


if __name__ == "__main__":
    main()
