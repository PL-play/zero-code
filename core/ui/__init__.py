"""UI adapters — kept separate from agent core state."""

from core.ui.bundled_process_frontend import install_bundled_process_frontend
from core.ui.textual_adapter import TUIAdapter

__all__ = ["TUIAdapter", "install_bundled_process_frontend"]
