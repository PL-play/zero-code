from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll, Vertical
from textual.widgets import DirectoryTree, TextArea, RichLog, Static, TabbedContent, TabPane, Markdown, Footer, Header, Label, Select
from textual.binding import Binding
from textual.message import Message
from textual import events, work
from textual.screen import ModalScreen
from pathlib import Path
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
        Binding("ctrl+r", "refresh_explorer", "Refresh Explorer", show=True),
        Binding("f5", "refresh_explorer", "Refresh Explorer", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main_split"):
            # Left Pane: Chat
            with Vertical(id="left_pane"):
                yield VerticalScroll(id="chat_history")
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
            Label(" Build ", classes="status-highlight"),
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
        self.query_one(ChatInput).focus()

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
        pass

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
        
        # Note: the worker dispatch will be added in step 2
        # For now, we mock it
        self.run_worker(self.process_agent_query(query), thread=True)

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
            # We must run agent_loop... but it's synchronous and expects global UI
            reply = agent_loop(self.history)
            elapsed = time.time() - start_time
            self.system_log(f"Agent reply received, took {elapsed:.2f}s")
            self.append_chat(reply, "agent", elapsed)
        except Exception as e:
            self.system_log(f"Agent error: {str(e)}")
            self.append_chat(f"**Error:** {str(e)}", "system")
        finally:
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
