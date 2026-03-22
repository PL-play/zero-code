from __future__ import annotations

from typing import Any, Optional


_UI: Any = None


def set_ui(ui: Any) -> None:
    global _UI
    _UI = ui


def get_ui() -> Any | None:
    return _UI


def safe_dispatch(method_name: str, *args: Any, **kwargs: Any) -> None:
    """Best-effort call into the current UI adapter (if any)."""
    ui = get_ui()
    if ui is None:
        return
    method = getattr(ui, method_name, None)
    if callable(method):
        method(*args, **kwargs)

