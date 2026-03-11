from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll, Vertical
from textual.widgets import DirectoryTree, TextArea, RichLog, Static, TabbedContent, TabPane, Markdown, Footer, Header, Label, Select, Input
from textual.binding import Binding
from textual.message import Message
from textual import events, work
from textual.screen import ModalScreen
from pathlib import Path
import asyncio
import os
import re
import time
import threading
import subprocess
from datetime import datetime
from rich.syntax import Syntax
from rich.markdown import Markdown as RichMarkdown
from core.attachments import (
    apply_attachment_parent_navigation,
    apply_attachment_suggestion,
    build_user_message,
    get_attachment_query_at_cursor,
    get_attachment_suggestions,
    message_preview_text,
)
from core.runtime import WORKDIR, AGENT_DIR, MODEL
import tempfile
import webbrowser


_BROWSER_OPENABLE_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".tif",
    ".tiff",
}


def _extract_mermaid_blocks(text: str) -> list[str]:
    """Extract all ```mermaid ... ``` code blocks from markdown text."""
    pattern = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
    return [m.group(1).strip() for m in pattern.finditer(text)]


def _open_mermaid_in_browser(blocks: list[str], title: str = "Mermaid Diagrams") -> str | None:
    """Generate a temp HTML with Mermaid.js CDN and open in default browser.
    Returns the path to the temp HTML file, or None on error."""
    if not blocks:
        return None
    diagrams_html = ""
    for i, block in enumerate(blocks, 1):
        diagrams_html += f'<div class="diagram"><h3>Diagram {i}</h3><pre class="mermaid">{block}</pre></div>\n'
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1a1a2e; color: #eee; padding: 2rem; }}
  .diagram {{ background: #16213e; border-radius: 12px; padding: 1.5rem; margin: 1.5rem 0; box-shadow: 0 4px 20px rgba(0,0,0,0.3); }}
  .diagram h3 {{ color: #00ffcc; margin-top: 0; }}
  .mermaid {{ display: flex; justify-content: center; }}
  h1 {{ color: #a855f7; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p style="color:#888">Found {len(blocks)} diagram(s). Rendered with Mermaid.js.</p>
{diagrams_html}
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, theme: 'dark' }});
</script>
</body>
</html>"""
    try:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="mermaid_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        webbrowser.open(f"file://{path}")
        return path
    except Exception:
        return None


def _open_local_paths_in_browser(paths: list[str | Path]) -> int:
    opened = 0
    for raw_path in paths:
        try:
            path = Path(raw_path).expanduser().resolve()
            if not path.exists():
                continue
            if webbrowser.open(path.as_uri()):
                opened += 1
        except Exception:
            continue
    return opened


def _is_browser_openable_path(path: Path) -> bool:
    return path.suffix.lower() in _BROWSER_OPENABLE_EXTENSIONS


def _open_path_in_browser(path: str | Path) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            return False
        return bool(webbrowser.open(resolved.as_uri()))
    except Exception:
        return False

class FileViewer(ModalScreen):
    """Screen to display file content."""
    
    BINDINGS = [
        Binding("escape", "dismiss", "Close File"),
        Binding("d", "toggle_diff", "Toggle Git Diff"),
        Binding("v", "toggle_ref_view", "Toggle Ref View"),
        Binding("m", "toggle_markdown", "Toggle Markdown"),
        Binding("g", "view_mermaid", "View Mermaid"),
    ]
    
    def __init__(self, filepath: Path, **kwargs):
        super().__init__(**kwargs)
        self.filepath = filepath
        self.show_diff = False
        self.original_text = ""
        self.original_lang = ""
        self.is_browser_openable_file = _is_browser_openable_path(filepath)
        self.repo_root: Path | None = None
        self.current_ref = "HEAD"
        self.show_ref_view = False
        self.is_markdown_file = False
        self.render_as_markdown = False
        
    def compose(self) -> ComposeResult:
        with Vertical(id="file_viewer_container"):
            yield Label("", id="file_viewer_title")
            yield Select(
                [("Loading history...", "LOADING")],
                prompt="Git Ref",
                allow_blank=False,
                id="file_viewer_history",
            )
            with VerticalScroll(id="file_viewer_scroll"):
                yield Static(id="file_viewer_code")
            yield Label(
                "💡 ESC close  |  D diff  |  V version  |  M markdown  |  G open/mermaid  |  Select chooses Git ref",
                id="file_viewer_footer",
                markup=False,
            )
            
    def on_mount(self):
        title = self.query_one("#file_viewer_title", Label)
        try:
            # show relative path from WORKDIR
            rel_path = self.filepath.relative_to(WORKDIR)
            title.update(f"📄 {rel_path}")
        except ValueError:
            title.update(f"📄 {self.filepath.absolute()}")

        self.repo_root = self._find_repo_root()
        history = self._load_git_history()
        history_select = self.query_one("#file_viewer_history", Select)
        if history:
            history_select.set_options(history)
            values = [item[1] for item in history]
            history_select.value = "HEAD" if "HEAD" in values else values[0]
            self.current_ref = str(history_select.value)
        else:
            history_select.set_options([("No Git history", "NO_GIT")])
            history_select.value = "NO_GIT"
            history_select.disabled = True

        if self.is_browser_openable_file:
            ext = self.filepath.suffix.lower() or "file"
            self.original_text = (
                f"Preview is not rendered inline for `{ext}` files.\n\n"
                f"Press G to open this file in your default browser."
            )
            self.original_lang = "text"
            self._render_code(self.original_text, self.original_lang)
            return
        
        try:
            self.original_text = self.filepath.read_text(encoding="utf-8")
            ext = self.filepath.suffix.lower()
            lang_map = {".py": "python", ".txt": "text", ".md": "markdown", ".json": "json", ".js": "javascript", ".ts": "typescript", ".html": "html", ".css": "css", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml"}
            if ext in lang_map:
                self.original_lang = lang_map[ext]
            else:
                self.original_lang = "text"
            if ext == ".md":
                self.is_markdown_file = True
                self.render_as_markdown = False
            self._render_code(self.original_text, self.original_lang)
        except Exception as e:
            self.original_text = f"Error reading file: {e}"
            self.original_lang = "text"
            self._render_code(self.original_text, self.original_lang)
            
    def action_dismiss(self):
        self.app.pop_screen()

    def _find_repo_root(self) -> Path | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.filepath.parent), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
        except Exception:
            return None
        return None

    def _load_git_history(self) -> list[tuple[str, str]]:
        if not self.repo_root:
            return []
        try:
            rel_path = self.filepath.relative_to(self.repo_root)
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "log",
                    "--pretty=format:%h %s",
                    "-n",
                    "30",
                    "--",
                    str(rel_path),
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
            if result.returncode != 0:
                return []
            options: list[tuple[str, str]] = [("HEAD", "HEAD")]
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 1)
                commit = parts[0]
                message = parts[1] if len(parts) > 1 else ""
                options.append((f"{commit}  {message}", commit))
            return options
        except Exception:
            return []

    def _render_code(self, content: str, language: str) -> None:
        code_widget = self.query_one("#file_viewer_code", Static)
        if self.render_as_markdown and language == "markdown":
            code_widget.update(RichMarkdown(content))
        else:
            code_widget.update(
                Syntax(
                    content,
                    language or "text",
                    theme="monokai",
                    line_numbers=True,
                    word_wrap=False,
                )
            )

    def _render_diff(self) -> None:
        if not self.repo_root:
            self._render_code("Current file is not inside a Git repository.", "text")
            return
        try:
            rel_path = self.filepath.relative_to(self.repo_root)
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "diff",
                    "--no-color",
                    self.current_ref,
                    "--",
                    str(rel_path),
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0:
                self._render_code(f"Git error:\n{result.stderr or result.stdout}", "text")
                return
            if not result.stdout.strip():
                self._render_code(f"No changes found against {self.current_ref} for this file.", "text")
                return
            self._render_code(result.stdout, "diff")
        except Exception as e:
            self._render_code(f"Error checking git diff: {e}", "text")

    def _render_ref_content(self) -> None:
        if not self.repo_root:
            self._render_code("Current file is not inside a Git repository.", "text")
            return
        if self.current_ref == "NO_GIT":
            self._render_code("No Git history available for this file.", "text")
            return
        try:
            rel_path = self.filepath.relative_to(self.repo_root).as_posix()
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "show",
                    f"{self.current_ref}:{rel_path}",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0:
                self._render_code(
                    f"Cannot read file at {self.current_ref}.\n{result.stderr or result.stdout}",
                    "text",
                )
                return
            self._render_code(result.stdout, self.original_lang)
        except Exception as e:
            self._render_code(f"Error reading selected version: {e}", "text")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("d", "D"):
            event.stop()
            event.prevent_default()
            self.action_toggle_diff()
        elif event.key in ("v", "V"):
            event.stop()
            event.prevent_default()
            self.action_toggle_ref_view()
        elif event.key in ("m", "M"):
            event.stop()
            event.prevent_default()
            self.action_toggle_markdown()
        elif event.key in ("g", "G"):
            event.stop()
            event.prevent_default()
            self.action_view_mermaid()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "file_viewer_history":
            return
        self.current_ref = str(event.value)
        if self.show_diff:
            if self.current_ref == "NO_GIT":
                self._render_code("No Git history available for this file.", "text")
            else:
                self._render_diff()
        elif self.show_ref_view:
            self._render_ref_content()

    def action_toggle_diff(self):
        self.show_ref_view = False
        self.show_diff = not self.show_diff
        if self.show_diff:
            if self.current_ref == "NO_GIT":
                self._render_code("Current file is not inside a Git repository.", "text")
            else:
                self._render_diff()
        else:
            self._render_code(self.original_text, self.original_lang)

    def action_toggle_ref_view(self):
        self.show_diff = False
        self.show_ref_view = not self.show_ref_view
        if self.show_ref_view:
            self._render_ref_content()
        else:
            self._render_code(self.original_text, self.original_lang)

    def action_toggle_markdown(self):
        if not self.is_markdown_file:
            return
        self.render_as_markdown = not self.render_as_markdown
        if self.show_diff:
            self._render_diff()
        elif self.show_ref_view:
            self._render_ref_content()
        else:
            self._render_code(self.original_text, self.original_lang)

    def action_view_mermaid(self):
        """Open the current file in browser when appropriate, otherwise handle Mermaid diagrams."""
        if self.is_browser_openable_file:
            if _open_path_in_browser(self.filepath):
                self.app.notify(f"Opened {self.filepath.name} in browser", timeout=3)
            else:
                self.app.notify(f"Failed to open {self.filepath.name} in browser", severity="error", timeout=3)
            return

        text = self.original_text
        blocks = _extract_mermaid_blocks(text)
        if not blocks:
            self.app.notify("No mermaid diagrams found in this file.", severity="warning", timeout=3)
            return
        try:
            rel = self.filepath.relative_to(WORKDIR)
        except ValueError:
            rel = self.filepath.name
        path = _open_mermaid_in_browser(blocks, title=f"Mermaid — {rel}")
        if path:
            self.app.notify(f"Opened {len(blocks)} diagram(s) in browser", timeout=3)
        else:
            self.app.notify("Failed to open mermaid diagrams", severity="error", timeout=3)

class ChatInput(TextArea):
    """A multi-line text area that submits on Enter and allows newlines with Shift+Enter or Ctrl+J."""

    ATTACHMENT_SUGGESTION_FETCH_LIMIT = 64
    ATTACHMENT_PREVIEW_WINDOW = 8
    
    BINDINGS = [
        Binding("ctrl+j", "newline", "New Line", show=False),
    ]

    class Submitted(Message):
        """Posted when enter is pressed."""
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SuggestionChanged(Message):
        """Posted when @ path suggestions change."""
        def __init__(self, query: str | None, suggestions: list[dict[str, str]], selected_index: int) -> None:
            self.query = query
            self.suggestions = suggestions
            self.selected_index = selected_index
            super().__init__()

    def on_mount(self):
        self.soft_wrap = True
        self._attachment_query: str | None = None
        self._attachment_suggestions: list[dict[str, str]] = []
        self._attachment_selected_index = 0

    def action_submit(self):
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(self.text))

    def action_newline(self):
        self.insert("\n")

    def _cursor_index(self) -> int:
        row, column = self.selection.start
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    def _refresh_attachment_suggestions(self) -> None:
        query = get_attachment_query_at_cursor(self.text, self._cursor_index())
        if query is None:
            self._attachment_query = None
            self._attachment_suggestions = []
            self._attachment_selected_index = 0
            self.post_message(self.SuggestionChanged(None, [], 0))
            return

        self._attachment_query = query
        self._attachment_suggestions = get_attachment_suggestions(
            query,
            limit=self.ATTACHMENT_SUGGESTION_FETCH_LIMIT,
        )
        self._attachment_selected_index = 0
        self.post_message(
            self.SuggestionChanged(
                self._attachment_query,
                self._attachment_suggestions,
                self._attachment_selected_index,
            )
        )

    def _move_attachment_selection(self, delta: int) -> None:
        if not self._attachment_suggestions:
            return
        self._attachment_selected_index = (self._attachment_selected_index + delta) % len(self._attachment_suggestions)
        self.post_message(
            self.SuggestionChanged(
                self._attachment_query,
                self._attachment_suggestions,
                self._attachment_selected_index,
            )
        )

    def _page_attachment_selection(self, delta: int) -> None:
        if not self._attachment_suggestions:
            return
        window = self.ATTACHMENT_PREVIEW_WINDOW
        max_index = len(self._attachment_suggestions) - 1
        self._attachment_selected_index = min(
            max(self._attachment_selected_index + delta * window, 0),
            max_index,
        )
        self.post_message(
            self.SuggestionChanged(
                self._attachment_query,
                self._attachment_suggestions,
                self._attachment_selected_index,
            )
        )

    def _apply_selected_attachment_suggestion(self) -> bool:
        if not self._attachment_suggestions:
            return False
        selected = self._attachment_suggestions[self._attachment_selected_index]["value"]
        self.text = apply_attachment_suggestion(self.text, selected, self._cursor_index())
        end_location = self.document.end
        self.move_cursor(end_location)
        self._refresh_attachment_suggestions()
        return True

    def _enter_selected_attachment_directory(self) -> bool:
        if not self._attachment_suggestions:
            return False
        selected = self._attachment_suggestions[self._attachment_selected_index]
        if selected.get("kind") != "dir":
            return False
        self.text = apply_attachment_suggestion(self.text, selected["value"], self._cursor_index())
        end_location = self.document.end
        self.move_cursor(end_location)
        self._refresh_attachment_suggestions()
        return True

    def _navigate_attachment_parent(self) -> bool:
        query = get_attachment_query_at_cursor(self.text, self._cursor_index())
        if not query or not query.endswith(("/", "\\")):
            return False
        updated = apply_attachment_parent_navigation(self.text, self._cursor_index())
        if updated == self.text:
            return False
        self.text = updated
        end_location = self.document.end
        self.move_cursor(end_location)
        self._refresh_attachment_suggestions()
        return True

    def action_paste(self) -> None:
        """Paste from the system clipboard (pbpaste on macOS)."""
        if self.read_only:
            return
        clipboard = ""
        try:
            result = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0 and result.stdout:
                clipboard = result.stdout
        except Exception:
            pass
        if clipboard:
            start, end = self.selection
            if res := self._replace_via_keyboard(clipboard, start, end):
                self.move_cursor(res.end_location)
                self._refresh_attachment_suggestions()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            if self._apply_selected_attachment_suggestion():
                event.stop()
                event.prevent_default()
                return
            event.stop()
            event.prevent_default()
            self.action_submit()
        elif event.key == "shift+enter":  # Shift+Enter handles newline
            event.stop()
            event.prevent_default()
            self.action_newline()
        elif event.key == "tab":
            if self._apply_selected_attachment_suggestion():
                event.stop()
                event.prevent_default()
                return
            await super()._on_key(event)
        elif event.key in ("up", "down") and self._attachment_suggestions:
            event.stop()
            event.prevent_default()
            self._move_attachment_selection(-1 if event.key == "up" else 1)
        elif event.key in ("pageup", "pagedown") and self._attachment_suggestions:
            event.stop()
            event.prevent_default()
            self._page_attachment_selection(-1 if event.key == "pageup" else 1)
        elif event.key == "right" and self._attachment_suggestions:
            if self._enter_selected_attachment_directory():
                event.stop()
                event.prevent_default()
                return
            await super()._on_key(event)
        elif event.key == "left":
            if self._navigate_attachment_parent():
                event.stop()
                event.prevent_default()
                return
            await super()._on_key(event)
        else:
            await super()._on_key(event)
            self._refresh_attachment_suggestions()

class TerminalInput(Input):
    """Single-line input for the embedded terminal. Enter runs the command."""

    class CommandSubmitted(Message):
        def __init__(self, command: str) -> None:
            self.command = command
            super().__init__()

    def __init__(self, **kwargs):
        super().__init__(placeholder="$ type a command and press Enter...", **kwargs)

    def action_paste(self) -> None:
        """Paste from the system clipboard."""
        clipboard = ""
        try:
            result = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0 and result.stdout:
                clipboard = result.stdout.strip()
        except Exception:
            pass
        if clipboard:
            self.insert_text_at_cursor(clipboard)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            cmd = self.value.strip()
            if cmd:
                self.post_message(self.CommandSubmitted(cmd))
                self.value = ""
        else:
            await super()._on_key(event)

class ZeroCodeApp(App):
    """A Cyberpunk-themed Textual UI for the zero-code agent."""

    CSS = """
    Screen {
        background: #0D0D11;
    }

    #main_split {
        height: 1fr;
    }

    #left_pane {

        width: 60%;
        height: 100%;
        border-right: solid #8A2BE2;
        layout: vertical;
    }

    #chat_history {
        height: 1fr;
        padding: 1;
        background: #1A1A24;
        overflow-y: auto;
    }

    #chat_input {
        min-height: 3;
        max-height: 6;
        height: auto;
        dock: bottom;
        border: solid #00FFCC;
        background: #0D0D11;
        color: #00FFCC;
    }

    #transient_panels {
        height: auto;
        padding: 0 1 1 1;
        background: #1A1A24;
    }

    #think_live {
        display: none;
        height: auto;
        max-height: 6;
        padding: 0 1;
        margin-bottom: 1;
        border: round #A855F7;
        background: #141220;
        color: #D8B4FE;
    }

    #tool_chain {
        display: none;
        height: auto;
        max-height: 8;
        padding: 0 1;
        border: round #FACC15;
        background: #1E1A10;
        color: #FDE68A;
    }

    #attachment_preview {
        display: none;
        height: auto;
        max-height: 8;
        padding: 0 1;
        margin-top: 1;
        border: round #55AAFF;
        background: #101826;
        color: #B9D7FF;
    }

    #right_pane {
        width: 40%;
        height: 100%;
        background: #111118;
    }

    TabbedContent {
        height: 1fr;
    }
    
    TabPane {
        height: 1fr;
        padding: 0;
        overflow: hidden;
    }

    /* Cyberpunk Tabs Styling */
    Tabs {
        background: #1A1A24;
    }

    Tab {
        color: #8888AA;
    }

    #agent_logs {
        height: 1fr;
        color: #AAAAAA;
        background: #111118;
        overflow-x: auto;
    }

    #todo_list {
        height: auto;
        padding: 1;
        margin-bottom: 1;
        border: solid #00FFCC;
        border-title-color: #00FFCC;
        background: #1A1A24;
    }

    .chat-user, .chat-agent {
        height: auto;
    }

    .chat-user {
        background: #1E2A1E;
        border: round #2E7D32;
    }

    .chat-agent {
        background: #1E1E28;
        border: none;
    }

    #status_bar {
        height: 1;
        background: #111118;
        color: #8888AA;
        width: 100%;
        margin-bottom: 0;
        layout: horizontal;
    }

    #status_spacer {
        width: 1fr;
    }

    .status-highlight {
        color: #55AAFF;
    }

    .status-dim {
        color: #555566;
    }

    .status-running {
        color: #FFD700;
        text-style: bold;
    }

    .agent-meta {
        color: #4a4a60;
        margin: 0 0 1 2;
    }

    #status_bar Label {
        margin-right: 2;
    }

    FileViewer {
        align: center middle;
        background: rgba(13, 13, 17, 0.8);
    }

    #file_viewer_container {
        width: 95%;
        height: 95%;
        border: solid #00FFCC;
        background: #111118;
        padding: 1;
    }
    
    #file_viewer_title {
        text-style: bold;
        color: #55AAFF;
        margin-bottom: 1;
    }

    #file_viewer_history {
        margin-bottom: 1;
    }

    #file_viewer_scroll {
        height: 1fr;
        border: round #2D2D3A;
        padding: 0 1;
        background: #0D0D11;
    }

    #file_viewer_code {
        height: auto;
    }
    
    #file_viewer_footer {
        margin-top: 1;
        color: #8888AA;
        text-align: center;
        width: 100%;
    }
    
    #file_changes {
        height: auto;
        padding: 1;
        margin-bottom: 1;
        border: solid #FACC15;
        border-title-color: #FACC15;
        background: #1A1A24;
    }

    #token_usage {
        height: auto;
        padding: 1;
        margin-bottom: 1;
        border: solid #00FFCC;
        border-title-color: #00FFCC;
        background: #1A1A24;
    }

    .diff-block {
        border-left: solid #FACC15;
        padding: 0 1;
        margin: 1 0;
        height: auto;
    }

    .diff-block Static {
        height: auto;
    }

    #terminal_output {
        height: 1fr;
        color: #CCCCCC;
        background: #0D0D11;
        overflow-x: auto;
    }

    #terminal_input {
        min-height: 3;
        max-height: 3;
        height: auto;
        border: solid #00FFCC;
        background: #0D0D11;
        color: #00FFCC;
    }

    #debug_logs {
        height: 1fr;
        color: #55AAFF;
        background: #111118;
        overflow-x: auto;
    }
    """

    ALLOW_SELECT = True

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("escape", "cancel_agent", "Stop Agent", show=True),
        Binding("ctrl+r", "refresh_explorer", "Refresh Explorer", show=True),
        Binding("f5", "refresh_explorer", "Refresh Explorer", show=False),
        Binding("ctrl+y", "copy_last_reply", "Copy Reply", show=True),
        Binding("ctrl+g", "open_mermaid", "Open Mermaid", show=True),
        Binding("ctrl+o", "open_last_image", "Open Image", show=True),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = []
        self._stream_chunk_count = 0
        self._stream_think_count = 0
        self._stream_text_buffer = ""
        self._stream_pending_buffer = ""
        self._stream_wrapper: Container | None = None
        self._stream_text_widget: Static | None = None
        self._stream_turn_started_at = 0.0
        self._stream_last_flush_ts = 0.0
        try:
            self._stream_min_flush_interval_s = max(
                0.01,
                float(os.getenv("STREAM_FLUSH_MIN_INTERVAL_S", "0.08")),
            )
        except Exception:
            self._stream_min_flush_interval_s = 0.08
        try:
            self._stream_min_flush_chars = max(
                1,
                int(os.getenv("STREAM_FLUSH_MIN_CHARS", "24")),
            )
        except Exception:
            self._stream_min_flush_chars = 24
        self._agent_running = False
        self._agent_cancel_event: threading.Event | None = None
        self._agent_task: asyncio.Task | None = None
        self._status_text = "Idle"
        self._spinner_idx = 0
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._think_live_buffer = ""
        self._tool_chain_entries: list[str] = []
        self._tool_chain_max_entries = 6
        try:
            self._think_hide_delay_s = max(0.0, float(os.getenv("THINK_PANEL_HIDE_DELAY_S", "1.8")))
        except Exception:
            self._think_hide_delay_s = 1.8
        try:
            self._tool_hide_delay_s = max(0.0, float(os.getenv("TOOL_PANEL_HIDE_DELAY_S", "2.2")))
        except Exception:
            self._tool_hide_delay_s = 2.2
        self._tool_chain_title = os.getenv("TOOL_CHAIN_TITLE", "Tool Execution Chain")
        self._think_hide_timer = None
        self._tool_hide_timer = None
        self._chat_input_placeholder = os.getenv("CHAT_INPUT_PLACEHOLDER", "请输入内容后按回车发送 (Shift+Enter 换行)")
        self._last_reply_text: str = ""
        self._pending_mermaid_blocks: list[str] = []
        self._pending_image_paths: list[str] = []
        self._terminal_bash = None  # lazy-init BashSession for Terminal tab

    def _cancel_timer(self, timer_obj):
        if timer_obj is None:
            return
        try:
            timer_obj.stop()
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main_split"):
            # Left Pane: Chat
            with Vertical(id="left_pane"):
                yield VerticalScroll(id="chat_history")
                with Vertical(id="transient_panels"):
                    yield Static("", id="think_live")
                    yield Static("", id="tool_chain")
                    yield Static("", id="attachment_preview")
                yield ChatInput(id="chat_input")

            # Right Pane: Tools/Tabs
            with Vertical(id="right_pane"):
                with TabbedContent(initial="tab-logs"):
                    with TabPane("Explorer", id="tab-explorer"):
                        yield DirectoryTree(str(WORKDIR), id="explorer_tree")
                    with TabPane("Agent Logs", id="tab-logs"):
                        # Re-enable Textual wrapping so Rich Tables respect exactly the remaining log width
                        yield RichLog(id="agent_logs", wrap=True, highlight=True, markup=True, auto_scroll=True)
                    with TabPane("Status", id="tab-status"):
                        with VerticalScroll():
                            yield Static("TODO", id="todo_list")
                            yield Static("[bold #FACC15]Files Changed:[/bold #FACC15]\n  [dim](none)[/dim]", id="file_changes", markup=True)
                            yield Static("[bold #00FFCC]Token Usage[/bold #00FFCC]\n[dim]No usage yet.[/dim]", id="token_usage", markup=True)
                    with TabPane("Terminal", id="tab-terminal"):
                        with Vertical():
                            yield RichLog(id="terminal_output", wrap=True, highlight=False, markup=True, auto_scroll=True)
                            yield TerminalInput(id="terminal_input")
                    with TabPane("Debug", id="tab-debug"):
                        yield RichLog(id="debug_logs", wrap=True, highlight=True, markup=True, auto_scroll=True)
        
        yield Horizontal(
            Label("Idle", id="run_status", classes="status-dim"),
            Label(f" {MODEL} "),
            Label("", id="git_branch", classes="status-highlight"),
            Label(f" {WORKDIR.name} ", classes="status-dim"),
            Static("", id="status_spacer"),
            Label("tab agents  ctrl+p commands", classes="status-dim"),
            Label(" ○ ZeroCode 1.0 ", classes="status-highlight"),
            id="status_bar"
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Header).tall = False
        welcome_md = f"""```text
███████╗███████╗██████╗  ██████╗         ██████╗ ██████╗ ██████╗ ███████╗
╚══███╔╝██╔════╝██╔══██╗██╔═══██╗       ██╔════╝██╔═══██╗██╔══██╗██╔════╝
  ███╔╝ █████╗  ██████╔╝██║   ██║█████╗ ██║     ██║   ██║██║  ██║█████╗  
 ███╔╝  ██╔══╝  ██╔══██╗██║   ██║╚════╝ ██║     ██║   ██║██║  ██║██╔══╝  
███████╗███████╗██║  ██║╚██████╔╝       ╚██████╗╚██████╔╝██████╔╝███████╗
╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝         ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝                                           
```

Type your request below to get started. Use `/help` for commands.
"""
        chat = self.query_one("#chat_history", VerticalScroll)
        chat.mount(Markdown(welcome_md))

        self.system_log("ZeroCodeApp mounted")
        self.system_log(f"Model: {MODEL}")
        self.system_log(f"Workdir: {WORKDIR}")
        
        self.agent_log(f"Initialized agent at {WORKDIR}")
        self.set_interval(0.12, self._tick_run_status)
        self.set_interval(10, self._periodic_git_refresh)
        self._refresh_git_info()
        self._set_think_visible(False)
        self._set_tool_chain_visible(False)
        input_widget = self.query_one(ChatInput)
        input_widget.placeholder = self._chat_input_placeholder
        input_widget.focus()

    def _agent_meta_line(self, duration: float | None = None) -> str:
        now = datetime.now().strftime("%H:%M:%S")
        if duration is not None:
            return f"{now}  {MODEL}  {duration:.1f}s"
        return f"{now}  {MODEL}"

    def _set_think_visible(self, visible: bool):
        try:
            widget = self.query_one("#think_live", Static)
            widget.styles.display = "block" if visible else "none"
        except Exception:
            pass

    def _set_tool_chain_visible(self, visible: bool):
        try:
            widget = self.query_one("#tool_chain", Static)
            widget.styles.display = "block" if visible else "none"
        except Exception:
            pass

    def _set_attachment_preview_visible(self, visible: bool):
        try:
            widget = self.query_one("#attachment_preview", Static)
            widget.styles.display = "block" if visible else "none"
        except Exception:
            pass

    def _render_attachment_preview(self, query: str | None, suggestions: list[dict[str, str]], selected_index: int):
        try:
            widget = self.query_one("#attachment_preview", Static)
        except Exception:
            return

        if not suggestions:
            widget.update("")
            self._set_attachment_preview_visible(False)
            return

        preview_window = getattr(ChatInput, "ATTACHMENT_PREVIEW_WINDOW", 8)
        window_start = max(0, selected_index - preview_window // 2)
        window_end = min(len(suggestions), window_start + preview_window)
        if window_end - window_start < preview_window:
            window_start = max(0, window_end - preview_window)

        lines = [f"[bold #55AAFF]Attach path[/bold #55AAFF] [dim]@{query or ''}[/dim]"]
        for index in range(window_start, window_end):
            item = suggestions[index]
            marker = "[bold #FACC15]>[/bold #FACC15]" if index == selected_index else "[dim]-[/dim]"
            lines.append(f"{marker} {item['label']}")
        lines.append(
            f"[dim]Showing {window_start + 1}-{window_end} of {len(suggestions)} | "
            f"Tab/Enter choose, Up/Down move, PgUp/PgDn jump[/dim]"
        )
        widget.update("\n".join(lines))
        self._set_attachment_preview_visible(True)

    def on_chat_input_suggestion_changed(self, event: ChatInput.SuggestionChanged) -> None:
        self._render_attachment_preview(event.query, event.suggestions, event.selected_index)

    def _render_tool_chain(self):
        try:
            widget = self.query_one("#tool_chain", Static)
        except Exception:
            return

        if not self._tool_chain_entries:
            widget.update("")
            self._set_tool_chain_visible(False)
            return

        lines = [self._tool_chain_title]
        for idx, entry in enumerate(self._tool_chain_entries):
            if idx > 0:
                lines.append("│")
            branch = "└─" if idx == len(self._tool_chain_entries) - 1 else "├─"
            lines.append(f"{branch} {entry}")

        widget.update("\n".join(lines))
        self._set_tool_chain_visible(True)

    def _tick_run_status(self):
        try:
            label = self.query_one("#run_status", Label)
        except Exception:
            return

        if self._agent_running:
            self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
            frame = self._spinner_frames[self._spinner_idx]
            label.set_classes("status-running")
            label.update(f"{frame} {self._status_text}")
        else:
            label.set_classes("status-dim")
            label.update(self._status_text or "Idle")

    def _refresh_git_info(self):
        try:
            branch_result = subprocess.run(
                ["git", "-C", str(WORKDIR), "branch", "--show-current"],
                capture_output=True, text=True, timeout=3,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

            dirty_count = 0
            if branch:
                status_result = subprocess.run(
                    ["git", "-C", str(WORKDIR), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=3,
                )
                if status_result.returncode == 0:
                    dirty_count = len([l for l in status_result.stdout.splitlines() if l.strip()])

            if branch:
                label_text = f" {branch}"
                if dirty_count > 0:
                    label_text += f" *{dirty_count}"
                label_text += " "
            else:
                label_text = ""

            try:
                git_label = self.query_one("#git_branch", Label)
                git_label.update(label_text)
            except Exception:
                pass
        except Exception:
            pass

    def refresh_git_info(self):
        """Public method callable from state.py via _safe_dispatch."""
        self._refresh_git_info()

    def _periodic_git_refresh(self):
        self._refresh_git_info()

    async def action_refresh_explorer(self) -> None:
        try:
            tree = self.query_one("#explorer_tree", DirectoryTree)
            await tree.reload()
            self.system_log("Explorer refreshed")
        except Exception as e:
            self.system_log(f"Explorer refresh failed: {e}")

    def append_chat(self, markdown_text: str, role: str = "agent", duration: float = None):
        """Appends a new markdown block to the chat history."""
        def _add_chat():
            chat = self.query_one("#chat_history", VerticalScroll)
            now = datetime.now().strftime("%H:%M:%S")
            
            # Wrap duration string if it exists
            dur_str = f"\n\n*(Took {duration:.2f}s)*" if duration is not None else ""
            
            if role == "user":
                wrapper = Container(Markdown(f"**user [{now}]>**\n{markdown_text}"), classes="chat-user")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)
            elif role == "agent_plain":
                text = (markdown_text or "").rstrip()
                self._last_reply_text = text
                meta = self._agent_meta_line(duration) if duration is not None else self._agent_meta_line()
                wrapper = Container(Markdown(text), Static(meta, classes="agent-meta"), classes="chat-agent")
            elif role == "think":
                wrapper = Container(Markdown(f"**think [{now}]>**\n{markdown_text}"), classes="chat-agent")
            elif role == "tool":
                wrapper = Container(Markdown(f"**tool [{now}]>**\n{markdown_text}"), classes="chat-agent")
            else:
                wrapper = Container(Markdown(f"**agent [{now}]>**\n{markdown_text}{dur_str}"), classes="chat-agent")

            chat.mount(wrapper)
            chat.scroll_end(animate=False)
            
        if self._thread_id == threading.get_ident():
            _add_chat()
        else:
            self.call_from_thread(_add_chat)

    def append_diff(self, path: str, summary: str, diff_body: str):
        """Append a colored diff block to the main chat timeline."""
        def _add_diff():
            chat = self.query_one("#chat_history", VerticalScroll)
            now = datetime.now().strftime("%H:%M:%S")

            title_widget = Static(f"[bold yellow]{summary}[/bold yellow]  [dim]{now}[/dim]", markup=True)

            if diff_body.strip():
                diff_widget = Static(Syntax(diff_body, "diff", theme="monokai", word_wrap=False))
            else:
                diff_widget = Static("[dim](no diff)[/dim]", markup=True)

            wrapper = Container(title_widget, diff_widget, classes="diff-block")
            chat.mount(wrapper)
            chat.scroll_end(animate=False)

        if self._thread_id == threading.get_ident():
            _add_diff()
        else:
            self.call_from_thread(_add_diff)

    def _ensure_stream_output_block(self):
        if self._stream_text_widget is not None and self._stream_wrapper is not None:
            return
        chat = self.query_one("#chat_history", VerticalScroll)
        self._stream_text_widget = Static("")
        self._stream_wrapper = Container(self._stream_text_widget, classes="chat-agent")
        chat.mount(self._stream_wrapper)
        chat.scroll_end(animate=False)

    def agent_log(self, text: str):
        """Append to the execution log in the right pane."""
        def _log():
            try:
                log = self.query_one("#agent_logs", RichLog)
                log.write(text)
            except Exception:
                pass
                
        if self._thread_id == threading.get_ident():
            _log()
        else:
            self.call_from_thread(_log)

    def system_log(self, text: str):
        """Append to the debug execution log."""
        def _log():
            try:
                log = self.query_one("#debug_logs", RichLog)
                now = datetime.now().strftime("%H:%M:%S")
                log.write(f"[{now}] {text}")
            except Exception:
                pass
                
        if self._thread_id == threading.get_ident():
            _log()
        else:
            self.call_from_thread(_log)

    def terminal_log(self, text: str):
        """Append output to the Terminal tab."""
        def _log():
            try:
                log = self.query_one("#terminal_output", RichLog)
                log.write(text)
            except Exception:
                pass
                
        if self._thread_id == threading.get_ident():
            _log()
        else:
            self.call_from_thread(_log)

    def set_status(self, text: str):
        self._status_text = text

    async def action_cancel_agent(self) -> None:
        if not self._agent_running or self._agent_cancel_event is None:
            return
        self._agent_cancel_event.set()
        self.set_status("Stopping...")
        self.system_log("Cancellation requested by ESC")
        self.append_chat("Stopping current agent task...", "system")

    async def action_copy_last_reply(self) -> None:
        text = self._last_reply_text
        if not text:
            self.notify("No reply to copy", severity="warning", timeout=2)
            return
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=3)
            self.notify("Copied to clipboard ✓", timeout=2)
        except Exception as e:
            self.notify(f"Copy failed: {e}", severity="error", timeout=3)

    async def action_open_mermaid(self) -> None:
        """Open pending mermaid diagrams (from last agent reply) in browser."""
        blocks = self._pending_mermaid_blocks
        if not blocks:
            # Try extracting from the last reply text
            if self._last_reply_text:
                blocks = _extract_mermaid_blocks(self._last_reply_text)
        if not blocks:
            self.notify("No Mermaid diagrams found in the last reply.", severity="warning", timeout=3)
            return
        path = _open_mermaid_in_browser(blocks, title="Mermaid — Agent Reply")
        if path:
            self.notify(f"Opened {len(blocks)} diagram(s) in browser", timeout=3)
        else:
            self.notify("Failed to open mermaid diagrams", severity="error", timeout=3)

    async def action_open_last_image(self) -> None:
        paths = list(self._pending_image_paths)
        if not paths:
            self.notify("No generated or edited image available to open.", severity="warning", timeout=3)
            return
        opened = _open_local_paths_in_browser(paths)
        if opened <= 0:
            self.notify("Failed to open image in browser.", severity="error", timeout=3)
            return
        if opened == 1:
            self.notify(f"Opened image in browser: {paths[0]}", timeout=3)
        else:
            self.notify(f"Opened {opened} images in browser", timeout=3)

    def set_pending_image_paths(self, paths: list[str], tool_name: str = "image tool"):
        filtered = [str(path) for path in paths if str(path).strip()]
        self._pending_image_paths = filtered
        if not filtered:
            return
        image_word = "image" if len(filtered) == 1 else "images"
        self.notify(
            f"{tool_name} produced {len(filtered)} {image_word} — press Ctrl+O to open in browser",
            timeout=5,
        )

    def stream_start(self):
        self._cancel_timer(self._think_hide_timer)
        self._think_hide_timer = None
        self._stream_chunk_count = 0
        self._stream_think_count = 0
        self._stream_turn_started_at = time.monotonic()
        self._think_live_buffer = ""
        self._stream_text_buffer = ""
        self._stream_pending_buffer = ""
        self._stream_wrapper = None
        self._stream_text_widget = None
        self._stream_last_flush_ts = time.monotonic()
        try:
            self.query_one("#think_live", Static).update("")
        except Exception:
            pass
        self._set_think_visible(False)

    def _flush_stream_pending(self):
        if not self._stream_pending_buffer:
            return
        self._stream_text_buffer += self._stream_pending_buffer
        self._stream_pending_buffer = ""
        self._ensure_stream_output_block()
        try:
            if self._stream_text_widget is not None:
                self._stream_text_widget.update(self._stream_text_buffer)
            chat = self.query_one("#chat_history", VerticalScroll)
            chat.scroll_end(animate=False)
        except Exception:
            pass
        self._stream_last_flush_ts = time.monotonic()

    def append_stream_text(self, text: str):
        if not text or not text.strip():
            return
        normalized = text.replace("\r", "")
        if not self._stream_text_buffer and not self._stream_pending_buffer:
            normalized = normalized.lstrip("\n")
        normalized = re.sub(r"\n[ \t]+\n", "\n\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        if not normalized.strip():
            return

        self._stream_chunk_count += 1
        self._stream_pending_buffer += normalized

        now = time.monotonic()
        should_flush = (
            "\n" in normalized
            or len(self._stream_pending_buffer) >= self._stream_min_flush_chars
            or (now - self._stream_last_flush_ts) >= self._stream_min_flush_interval_s
        )
        if should_flush:
            self._flush_stream_pending()

    def append_stream_think(self, think: str):
        self._cancel_timer(self._think_hide_timer)
        self._think_hide_timer = None
        self._stream_think_count += 1
        self._think_live_buffer += think
        # Keep only the last N lines so the panel always shows the latest text
        # (max-height is 6; 1 line for "thinking…" header → 5 lines of content)
        _max_visible_lines = 5
        lines = self._think_live_buffer.strip().splitlines()
        if len(lines) > _max_visible_lines:
            self._think_live_buffer = "\n".join(lines[-_max_visible_lines:]) + "\n"
        try:
            widget = self.query_one("#think_live", Static)
            widget.update(f"thinking…\n{self._think_live_buffer.strip()}")
        except Exception:
            pass
        self._set_think_visible(bool(self._think_live_buffer.strip()))

    def append_tool_brief(self, brief: str):
        self._cancel_timer(self._tool_hide_timer)
        self._tool_hide_timer = None
        self._tool_chain_entries.append(brief)
        if len(self._tool_chain_entries) > self._tool_chain_max_entries:
            self._tool_chain_entries = self._tool_chain_entries[-self._tool_chain_max_entries :]
        self._render_tool_chain()

    def stream_end(self):
        self._flush_stream_pending()
        if self._stream_text_buffer.strip():
            self._last_reply_text = self._stream_text_buffer.rstrip()
            # Render final streaming output as Markdown
            if self._stream_text_widget is not None:
                try:
                    self._stream_text_widget.update(RichMarkdown(self._last_reply_text))
                except Exception:
                    pass
            # Auto-detect mermaid blocks and offer to open in browser
            mermaid_blocks = _extract_mermaid_blocks(self._last_reply_text)
            if mermaid_blocks:
                self._pending_mermaid_blocks = mermaid_blocks
                self.notify(
                    f"Found {len(mermaid_blocks)} Mermaid diagram(s) — press Ctrl+G to open in browser",
                    timeout=6,
                )
        if self._think_live_buffer.strip():
            self._cancel_timer(self._think_hide_timer)

            def _hide_think():
                self._think_live_buffer = ""
                try:
                    self.query_one("#think_live", Static).update("")
                except Exception:
                    pass
                self._set_think_visible(False)

            self._think_hide_timer = self.set_timer(self._think_hide_delay_s, _hide_think)
        else:
            self._set_think_visible(False)
        self._stream_wrapper = None
        self._stream_text_widget = None
        self._stream_text_buffer = ""
        self._stream_pending_buffer = ""

    def _finalize_stream_meta(self, elapsed: float):
        """Mount a meta footer below the last agent reply in the chat."""
        meta_text = self._agent_meta_line(elapsed)

        def _mount():
            try:
                chat = self.query_one("#chat_history", VerticalScroll)
                meta = Static(meta_text, classes="agent-meta")
                chat.mount(meta)
                chat.scroll_end(animate=False)
            except Exception:
                pass

        self.call_later(_mount)

    def set_round_tools_present(self, has_tools: bool):
        if has_tools:
            return
        if not self._tool_chain_entries:
            self._set_tool_chain_visible(False)
            return

        self._cancel_timer(self._tool_hide_timer)

        def _hide_tool_chain():
            self._tool_chain_entries = []
            try:
                self.query_one("#tool_chain", Static).update("")
            except Exception:
                pass
            self._set_tool_chain_visible(False)

        self._tool_hide_timer = self.set_timer(self._tool_hide_delay_s, _hide_tool_chain)

    def _cleanup_after_cancel(self):
        """Clean up UI panels and message history after ESC cancellation."""
        # --- hide transient panels immediately ---
        self._cancel_timer(self._think_hide_timer)
        self._think_hide_timer = None
        self._think_live_buffer = ""
        try:
            self.query_one("#think_live", Static).update("")
        except Exception:
            pass
        self._set_think_visible(False)

        self._cancel_timer(self._tool_hide_timer)
        self._tool_hide_timer = None
        self._tool_chain_entries = []
        try:
            self.query_one("#tool_chain", Static).update("")
        except Exception:
            pass
        self._set_tool_chain_visible(False)

        # reset stream state
        self._stream_wrapper = None
        self._stream_text_widget = None
        self._stream_text_buffer = ""
        self._stream_pending_buffer = ""

        # --- sanitize message history ---
        self._sanitize_history_after_cancel()

    def _sanitize_history_after_cancel(self):
        """Ensure messages don't have orphan tool_calls without matching tool results.

        After cancellation the assistant message with tool_calls is already in
        self.history, but some (or all) of the corresponding role='tool'
        responses may be missing.  The API requires every tool_call to have a
        matching tool result.  We fill in stubs for missing ones so the next
        request won't fail with error 2013.
        """
        msgs = self.history
        if not msgs:
            return

        # Walk backwards to find the last assistant message with tool_calls
        last_asst_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
                last_asst_idx = i
                break

        if last_asst_idx is None:
            return  # nothing to fix

        tool_calls = msgs[last_asst_idx]["tool_calls"]
        expected_ids = set()
        call_by_id: dict[str, dict] = {}
        for tc in tool_calls:
            tc_id = str(tc.get("id") or "")
            expected_ids.add(tc_id)
            call_by_id[tc_id] = tc

        # Collect tool result ids that already follow the assistant msg
        present_ids: set[str] = set()
        for msg in msgs[last_asst_idx + 1 :]:
            if msg.get("role") == "tool":
                present_ids.add(str(msg.get("tool_call_id") or ""))
            else:
                break  # stop at first non-tool message

        missing_ids = expected_ids - present_ids
        if not missing_ids:
            return  # all tool results present — nothing to fix

        # Append stub results for each missing tool_call
        for tc_id in missing_ids:
            tc = call_by_id.get(tc_id, {})
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = (fn.get("name") if isinstance(fn, dict) else None) or "unknown"
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": "[cancelled by user]",
                }
            )

    def update_todos(self, todo_text: str):
        def _update():
            try:
                self.query_one("#todo_list", Static).update(todo_text)
            except Exception:
                pass
                
        if self._thread_id == threading.get_ident():
            _update()
        else:
            self.call_from_thread(_update)

    def update_file_changes(self, text: str):
        def _update():
            try:
                self.query_one("#file_changes", Static).update(text)
            except Exception:
                pass

        if self._thread_id == threading.get_ident():
            _update()
        else:
            self.call_from_thread(_update)

    def update_usage(self, usage_text: str):
        def _update():
            try:
                self.query_one("#token_usage", Static).update(usage_text)
            except Exception:
                pass
                
        if self._thread_id == threading.get_ident():
            _update()
        else:
            self.call_from_thread(_update)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Handle enter/click on file tree."""
        event.stop()
        if event.path.is_file():
            self.push_screen(FileViewer(event.path))

    async def on_terminal_input_command_submitted(self, event: TerminalInput.CommandSubmitted) -> None:
        """Run a shell command typed in the Terminal tab."""
        cmd = event.command
        if not cmd:
            return
        log = self.query_one("#terminal_output", RichLog)
        log.write(f"[bold #00FFCC]$[/bold #00FFCC] {cmd}")

        # Lazy-init a dedicated BashSession for Terminal tab
        if self._terminal_bash is None:
            from core.tools import BashSession
            self._terminal_bash = BashSession(WORKDIR)

        bash = self._terminal_bash

        def _run():
            return bash.execute(cmd, timeout=30)

        try:
            output = await asyncio.to_thread(_run)
        except Exception as e:
            output = f"Error: {e}"

        # Strip the "exit_code=N\n" prefix and display cleanly
        lines = output.split("\n", 1)
        if lines[0].startswith("exit_code="):
            code = lines[0].split("=", 1)[1]
            body = lines[1] if len(lines) > 1 else ""
            # Strip stdout:/stderr: labels for cleaner display
            body = body.replace("stdout:\n", "").replace("stderr:\n", "")
            if code != "0":
                log.write(f"{body}\n[bold #FF5555]exit {code}[/bold #FF5555]")
            else:
                log.write(body if body.strip() else "[dim](no output)[/dim]")
        else:
            log.write(output)

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return

        if self._agent_running:
            self.append_chat("Agent is running. Press ESC to stop current task.", "system")
            return

        input_widget = self.query_one(ChatInput)
        input_widget.text = ""

        if query.lower() in ("q", "exit", "quit"):
            self.exit()
            return

        from core.commands import rewrite_attach_command

        rewritten_query = rewrite_attach_command(query)
        if query.startswith("/attach") and rewritten_query is None:
            self.append_chat("Usage: `/attach <path> [prompt]`", "system")
            return
        query = rewritten_query or query
            
        user_message, warnings = build_user_message(query)
        self.append_chat(message_preview_text(user_message), "user")
        for warning in warnings:
            self.append_chat(warning, "system")
        
        # Determine if it's a slash command or regular query
        # We will dispatch this to the agent thread
        if query.startswith("/"):
            # For now, handle it natively (will refactor later)
            self._handle_command(query)
            return

        # Disable input while processing
        input_widget.disabled = True
        self._agent_running = True
        self._agent_cancel_event = threading.Event()
        self.set_status("Running")
        
        self._agent_task = asyncio.create_task(self.process_agent_query(user_message))

    def _handle_command(self, query: str):
        from core.commands import COMMAND_DISPATCH, SLASH_COMMANDS
        cmd_name = query.split()[0]
        handler = COMMAND_DISPATCH.get(cmd_name)
        if handler:
            # We call the handler with history mapping
            self.agent_log(f"Executed command {cmd_name}")
            try:
                handler(raw_query=query, history=self.history)
            except Exception as e:
                self.agent_log(f"[bold red]Command error:[/bold red] {e}")
        else:
            known = ", ".join(c["name"] for c in SLASH_COMMANDS)
            self.append_chat(f"Unknown command: `{cmd_name}`. Available: {known}", "system")

    async def process_agent_query(self, user_message: dict):
        from core.agent import agent_loop
        self.history.append(user_message)
        query = message_preview_text(user_message)
        
        start_time = time.time()
        self.system_log(f"Starting agent query: {query}")
        try:
            cancel_event = self._agent_cancel_event or threading.Event()
            reply = await asyncio.to_thread(agent_loop, self.history, cancel_event)
            elapsed = time.time() - start_time
            self.system_log(f"Agent reply received, took {elapsed:.2f}s")
            if reply == "[cancelled by user]":
                self._cleanup_after_cancel()
                self.append_chat("Agent task cancelled.", "system")
            elif self._stream_chunk_count == 0:
                self.append_chat(reply, "agent_plain", elapsed)
            else:
                self._finalize_stream_meta(elapsed)
        except Exception as e:
            self.system_log(f"Agent error: {str(e)}")
            self.append_chat(f"**Error:** {str(e)}", "system")
        finally:
            self._agent_running = False
            self._agent_cancel_event = None
            self._agent_task = None
            self.set_status("Idle")
            self._enable_input_from_thread()

    def _enable_input_from_thread(self):
        def _enable():
            input_w = self.query_one(ChatInput)
            input_w.disabled = False
            input_w.focus()
            
        if self._thread_id == threading.get_ident():
            _enable()
        else:
            self.call_from_thread(_enable)
