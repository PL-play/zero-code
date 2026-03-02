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

__all__ = [
    "WORKDIR",
    "AGENT_DIR",
    "SKILLS_DIR",
    "MODEL",
    "client",
    "main",
]


def main():
    UI.welcome()
    _handle_help()
    history = []
    while True:
        try:
            query = UI.get_input()
        except (EOFError, KeyboardInterrupt):
            UI.console.print("\n[dim]Bye.[/dim]")
            break

        stripped = query.strip().lower()
        if stripped in ("q", "exit", "quit", ""):
            UI.console.print("[dim]Bye.[/dim]")
            break

        if stripped.startswith("/"):
            cmd_name = stripped.split()[0]
            handler = COMMAND_DISPATCH.get(cmd_name)
            if handler:
                handler(raw_query=query, history=history)
            else:
                known = ", ".join(c["name"] for c in SLASH_COMMANDS)
                UI.console.print(
                    f"[dim]{UI._ts()}[/dim] [bold red]Unknown command:[/bold red] {cmd_name}  (available: {known})"
                )
            continue

        history.append({"role": "user", "content": query})
        try:
            reply = agent_loop(history)
        except Exception as exc:
            UI.error(str(exc))
            continue
        UI.show_reply(reply)


if __name__ == "__main__":
    main()
