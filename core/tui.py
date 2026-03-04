from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll, Vertical
from textual.widgets import DirectoryTree, TextArea, RichLog, Static, TabbedContent, TabPane, Markdown, Footer, Header, Label, Select
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
from core.runtime import WORKDIR, AGENT_DIR, MODEL

class FileViewer(ModalScreen):
    """Screen to display file content."""
    
    BINDINGS = [
        Binding("escape", "dismiss", "Close File"),
        Binding("d", "toggle_diff", "Toggle Git Diff"),
        Binding("v", "toggle_ref_view", "Toggle Ref View"),
    ]
    
    def __init__(self, filepath: Path, **kwargs):
        super().__init__(**kwargs)
        self.filepath = filepath
        self.show_diff = False
        self.original_text = ""
        self.original_lang = ""
        self.repo_root: Path | None = None
        self.current_ref = "HEAD"
        self.show_ref_view = False
        
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
                "💡 ESC close  |  D toggle diff  |  V view selected version  |  Select chooses Git ref",
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
        
        try:
            self.original_text = self.filepath.read_text(encoding="utf-8")
            ext = self.filepath.suffix.lower()
            lang_map = {".py": "python", ".txt": "text", ".md": "markdown", ".json": "json", ".js": "javascript", ".ts": "typescript", ".html": "html", ".css": "css", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml"}
            if ext in lang_map:
                self.original_lang = lang_map[ext]
            else:
                self.original_lang = "text"
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

class ChatInput(TextArea):
    """A multi-line text area that submits on Enter and allows newlines with Shift+Enter or Ctrl+J."""
    
    BINDINGS = [
        Binding("ctrl+j", "newline", "New Line", show=False),
    ]

    class Submitted(Message):
        """Posted when enter is pressed."""
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def on_mount(self):
        self.soft_wrap = True

    def action_submit(self):
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(self.text))

    def action_newline(self):
        self.insert("\n")

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
        elif event.key == "shift+enter":  # Shift+Enter handles newline
            event.stop()
            event.prevent_default()
            self.action_newline()
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
    
    #debug_logs {
        height: 1fr;
        color: #55AAFF;
        background: #111118;
        overflow-x: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("escape", "cancel_agent", "Stop Agent", show=True),
        Binding("ctrl+r", "refresh_explorer", "Refresh Explorer", show=True),
        Binding("f5", "refresh_explorer", "Refresh Explorer", show=False),
        Binding("ctrl+y", "copy_last_reply", "Copy Reply", show=True),
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
                            yield Static("Token Usage:\nNo usage yet.", id="token_usage")
                    with TabPane("Debug", id="tab-debug"):
                        yield RichLog(id="debug_logs", wrap=True, highlight=True, markup=True, auto_scroll=True)
        
        yield Horizontal(
            Label("Idle", id="run_status", classes="status-dim"),
            Label(f" {MODEL} "),
            Label(f" 📂 {WORKDIR} ", classes="status-dim"),
            Label(" ZeroCode Zen ", classes="status-dim"),
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
        
        # Log welcome message to the agent logs as well
        self.agent_log(f"Initialized agent at {WORKDIR}")
        self.set_interval(0.12, self._tick_run_status)
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
                wrapper.styles.border_left = ("solid", "green")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)
            elif role == "agent_plain":
                text = (markdown_text or "").rstrip()
                self._last_reply_text = text
                meta = self._agent_meta_line(duration) if duration is not None else self._agent_meta_line()
                wrapper = Container(Static(text), Static(meta, classes="agent-meta"), classes="chat-agent")
                wrapper.styles.border_left = ("solid", "blue")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)
            elif role == "think":
                wrapper = Container(Markdown(f"**think [{now}]>**\n{markdown_text}"), classes="chat-agent")
                wrapper.styles.border_left = ("solid", "magenta")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)
            elif role == "tool":
                wrapper = Container(Markdown(f"**tool [{now}]>**\n{markdown_text}"), classes="chat-agent")
                wrapper.styles.border_left = ("solid", "yellow")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)
            else:
                wrapper = Container(Markdown(f"**agent [{now}]>**\n{markdown_text}{dur_str}"), classes="chat-agent")
                wrapper.styles.border_left = ("solid", "blue")
                wrapper.styles.padding = (0, 1)
                wrapper.styles.margin = (1, 0)

            chat.mount(wrapper)
            chat.scroll_end(animate=False)
            
        if self._thread_id == threading.get_ident():
            _add_chat()
        else:
            self.call_from_thread(_add_chat)

    def _ensure_stream_output_block(self):
        if self._stream_text_widget is not None and self._stream_wrapper is not None:
            return
        chat = self.query_one("#chat_history", VerticalScroll)
        self._stream_text_widget = Static("")
        self._stream_wrapper = Container(self._stream_text_widget, classes="chat-agent")
        self._stream_wrapper.styles.border_left = ("solid", "blue")
        self._stream_wrapper.styles.padding = (0, 1)
        self._stream_wrapper.styles.margin = (1, 0)
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
            
        self.append_chat(query, "user")
        
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
        
        self._agent_task = asyncio.create_task(self.process_agent_query(query))

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

    async def process_agent_query(self, query: str):
        from core.agent import agent_loop
        self.history.append({"role": "user", "content": query})
        
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
