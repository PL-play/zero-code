from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll, Vertical
from textual.widgets import DirectoryTree, TextArea, RichLog, Static, TabbedContent, TabPane, Markdown, Footer, Header, Label
from textual.binding import Binding
from textual.message import Message
from textual import events
from pathlib import Path
from core.runtime import WORKDIR, AGENT_DIR, MODEL

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
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
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
                        yield DirectoryTree(str(WORKDIR))
                    with TabPane("Agent Logs", id="tab-logs"):
                        # Re-enable Textual wrapping so Rich Tables respect exactly the remaining log width
                        yield RichLog(id="agent_logs", wrap=True, highlight=True, markup=True, auto_scroll=True)
                    with TabPane("Status", id="tab-status"):
                        with VerticalScroll():
                            yield Static("TODO", id="todo_list")
                            yield Static("Token Usage:\nNo usage yet.", id="token_usage")
        
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
        
        # Log welcome message to the agent logs as well
        self.agent_log(f"Initialized agent at {WORKDIR}")
        self.query_one(ChatInput).focus()

    def append_chat(self, markdown_text: str, role: str = "agent"):
        """Appends a new markdown block to the chat history."""
        chat = self.query_one("#chat_history", VerticalScroll)
        if role == "user":
            wrapper = Container(Markdown(f"**user>**\n{markdown_text}"), classes="chat-user")
            wrapper.styles.border_left = ("solid", "green")
            wrapper.styles.padding = (0, 1)
            wrapper.styles.margin = (1, 0)
        else:
            wrapper = Container(Markdown(f"**agent>**\n{markdown_text}"), classes="chat-agent")
            wrapper.styles.border_left = ("solid", "blue")
            wrapper.styles.padding = (0, 1)
            wrapper.styles.margin = (1, 0)

        chat.mount(wrapper)
        chat.scroll_end(animate=False)

    def agent_log(self, text: str):
        """Append to the execution log in the right pane."""
        log = self.query_one("#agent_logs", RichLog)
        log.write(text)

    def set_status(self, text: str):
        # We can implement a clean status bar in the Header or Footer
        # For now we just log it cleanly
        pass

    def update_todos(self, todo_text: str):
        self.query_one("#todo_list", Static).update(todo_text)

    def update_usage(self, usage_text: str):
        self.query_one("#token_usage", Static).update(usage_text)

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
        
        try:
            # We must run agent_loop... but it's synchronous and expects global UI
            reply = agent_loop(self.history)
            self.call_from_thread(self.append_chat, reply, "agent")
        except Exception as e:
            self.call_from_thread(self.append_chat, f"**Error:** {str(e)}", "system")
        finally:
            self.call_from_thread(self._enable_input)

    def _enable_input(self):
        input_w = self.query_one(ChatInput)
        input_w.disabled = False
        input_w.focus()
