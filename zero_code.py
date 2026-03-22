#!/usr/bin/env python3
"""
Thin entrypoint for zero-code.

Only keeps:
- main() entry
- minimal runtime config exports
"""

from core.application import AgentLoop, Config
from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKDIR, WORKSPACE_DIR, client
from core.ui.bundled_process_frontend import install_bundled_process_frontend

__all__ = [
    "WORKSPACE_DIR",
    "WORKDIR",
    "AGENT_DIR",
    "SKILLS_DIR",
    "MODEL",
    "client",
    "main",
    "Config",
    "AgentLoop",
]


def main():
    """Generic config + bundled in-process UI add-on; core stays UI-agnostic."""
    cfg = Config()
    install_bundled_process_frontend(cfg)
    AgentLoop(cfg).run()


if __name__ == "__main__":
    main()
