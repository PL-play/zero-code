#!/usr/bin/env python3
"""
zero_code.py - Single-file CLI code agent with rich console UI.

Features:
- 4 base tools: bash, read_file, write_file, edit_file
- TodoManager: structured task tracking with nag reminder
- SkillLoader: two-layer skill injection
- Subagent: fresh-context child agent for delegation
- Rich console UI: tool area (fixed height, auto-clear), persistent todo panel, timestamps
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd().resolve()
SKILLS_DIR = WORKDIR / "skills"

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ---------------------------------------------------------------------------
# Console UI
# ---------------------------------------------------------------------------

TOOL_MAX_LINES = 20


class ConsoleUI:
    """Rich terminal UI with replaceable tool area and persistent todo panel."""

    def __init__(self):
        self.console = Console()
        self._tool_area_height = 0
        self._todo_area_height = 0
        self._current_todo = ""
        self._tool_buffer: list[str] = []

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _measure(self, renderable) -> int:
        buf = StringIO()
        Console(file=buf, width=self.console.width, no_color=True).print(renderable)
        return buf.getvalue().count("\n")

    def _clear_up(self, n: int):
        if n <= 0:
            return
        sys.stdout.write(f"\033[{n}A\033[J")
        sys.stdout.flush()

    def _clear_dynamic(self):
        total = self._tool_area_height + self._todo_area_height
        self._clear_up(total)
        self._tool_area_height = 0
        self._todo_area_height = 0

    # -- tool area ----------------------------------------------------------

    def _render_tool_panel(self):
        if not self._tool_buffer:
            return
        visible = self._tool_buffer[-TOOL_MAX_LINES:]
        if len(self._tool_buffer) > TOOL_MAX_LINES:
            hidden = len(self._tool_buffer) - TOOL_MAX_LINES
            body_text = f"  ... {hidden} lines above ...\n" + "\n".join(visible)
        else:
            body_text = "\n".join(visible)

        panel = Panel(
            Text(body_text),
            title=f"[bold cyan]Tools[/bold cyan] [dim]({len(self._tool_buffer)} entries)[/dim]",
            subtitle=f"[dim]{self._ts()}[/dim]",
            border_style="cyan",
            padding=(0, 1),
        )
        self._tool_area_height = self._measure(panel)
        self.console.print(panel)

    def _render_todo(self):
        if not self._current_todo or self._current_todo == "No todos.":
            self._todo_area_height = 0
            return
        panel = Panel(
            Text(self._current_todo),
            title="[bold green]TODO[/bold green]",
            subtitle=f"[dim]{self._ts()}[/dim]",
            border_style="green",
            padding=(0, 1),
        )
        self._todo_area_height = self._measure(panel)
        self.console.print(panel)

    def _refresh(self):
        self._clear_dynamic()
        self._render_tool_panel()
        self._render_todo()

    def new_tool_cycle(self):
        self._tool_buffer = []

    def tool_call(self, name: str, output: str, is_sub: bool = False):
        ts = self._ts()
        prefix = "  sub" if is_sub else "    "
        out_preview = output.replace("\n", " ")
        if len(out_preview) > 120:
            out_preview = out_preview[:117] + "..."
        entry = f"{ts} {prefix} {name}: {out_preview}"
        self._tool_buffer.append(entry)
        self._refresh()

    def task_start(self, desc: str, prompt_preview: str):
        ts = self._ts()
        if len(prompt_preview) > 100:
            prompt_preview = prompt_preview[:97] + "..."
        self._tool_buffer.append(f"{ts}      sub_agent ({desc}): {prompt_preview}")
        self._refresh()

    def subagent_text(self, text: str):
        ts = self._ts()
        preview = text.replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        self._tool_buffer.append(f"{ts}   sub> {preview}")
        self._refresh()

    def subagent_limit(self, max_rounds: int):
        ts = self._ts()
        self._tool_buffer.append(f"{ts}   sub> [hit {max_rounds}-round limit, forcing summary]")
        self._refresh()

    # -- todo ---------------------------------------------------------------

    def update_todo(self, text: str):
        self._clear_up(self._todo_area_height)
        self._todo_area_height = 0
        self._current_todo = text
        self._render_todo()

    # -- messages -----------------------------------------------------------

    def show_reply(self, text: str):
        self._clear_dynamic()
        self._tool_buffer = []
        self._current_todo = ""
        self.console.print()
        panel = Panel(
            text,
            title=f"[bold blue]agent[/bold blue]",
            subtitle=f"[dim]{self._ts()}[/dim]",
            border_style="blue",
            padding=(0, 1),
        )
        self.console.print(panel)
        self.console.print()

    def get_input(self) -> str:
        return self.console.input(f"[dim]{self._ts()}[/dim] [bold green]you>[/bold green] ")

    def welcome(self):
        self.console.print()
        table = Table(show_header=False, border_style="blue", padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Workspace", str(WORKDIR))
        table.add_row("Model", MODEL)
        table.add_row("Exit", "type 'exit' or 'quit'")
        self.console.print(
            Panel(
                table,
                title="[bold blue]zero-code[/bold blue]",
                border_style="blue",
                padding=(1, 2),
            )
        )
        self.console.print()

    def error(self, text: str):
        self.console.print(f"[dim]{self._ts()}[/dim] [bold red]error:[/bold red] {text}")

    def nag_reminder(self):
        ts = self._ts()
        self._tool_buffer.append(f"{ts}      [reminder: update your todos]")
        self._refresh()


UI = ConsoleUI()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ---------------------------------------------------------------------------
# TodoManager
# ---------------------------------------------------------------------------

class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        self.items = validated
        result = self.render()
        UI.update_todo(result)
        return result

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


# ---------------------------------------------------------------------------
# SkillLoader
# ---------------------------------------------------------------------------

class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.glob("*/SKILL.md")):
            name = f.parent.name
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_frontmatter(self, text: str) -> tuple:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1))
            if not isinstance(meta, dict):
                meta = {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            rel_path = Path(skill["path"]).relative_to(WORKDIR)
            line = f"  - {name}: {desc} (path: {rel_path})"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        rel_path = Path(skill["path"]).relative_to(WORKDIR)
        return (
            f"<skill name=\"{name}\" path=\"{rel_path}\">\n"
            f"Source: {rel_path}\n\n"
            f"{skill['body']}\n"
            f"</skill>"
        )


SKILL_LOADER = SkillLoader(SKILLS_DIR)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Use the sub_agent tool to delegate exploration or subtasks to a subagent.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.

Skills available:
{SKILL_LOADER.get_descriptions()}"""

SUBAGENT_SYSTEM = f"""You are a coding subagent at {WORKDIR}.
Use load_skill when needed. Complete the given task, then summarize your findings."""


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Dispatch map
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "load_skill",
        "description": "Load specialized knowledge by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name to load"}},
            "required": ["name"],
        },
    },
]

CHILD_TOOLS = BASE_TOOLS

PARENT_TOOLS = BASE_TOOLS + [
    {
        "name": "sub_agent",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string", "description": "Short label for logging"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "todo",
        "description": "Update task list. Track progress on multi-step tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["id", "text", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
]


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------

def run_subagent(prompt: str, max_rounds: int = 30) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    response = None
    hit_limit = False

    for _ in range(max_rounds):
        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        assistant_msg = {
            "role": "assistant",
            "content": [block.model_dump() for block in response.content],
        }
        sub_messages.append(assistant_msg)

        for block in response.content:
            if hasattr(block, "text") and block.text:
                UI.subagent_text(block.text)

        has_tool_use = any(
            getattr(block, "type", None) == "tool_use" for block in response.content
        )
        if not has_tool_use:
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            except Exception as e:
                output = f"Error: {e}"
            UI.tool_call(block.name, output, is_sub=True)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)[:50000],
            })
        sub_messages.append({"role": "user", "content": results})
    else:
        hit_limit = True

    if response is None:
        return "(no summary)"

    if hit_limit:
        UI.subagent_limit(max_rounds)
        sub_messages.append({
            "role": "user",
            "content": (
                f"You have reached the maximum of {max_rounds} tool-call rounds and must stop now.\n"
                "Please summarize:\n"
                "1) What you have accomplished so far\n"
                "2) Key findings from your tool calls\n"
                "3) What remains unfinished or uncertain\n"
                "Be concise. Note clearly that this task may NOT be fully completed."
            ),
        })
        summary_response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=[],
            max_tokens=8000,
        )
        text = "".join(b.text for b in summary_response.content if hasattr(b, "text"))
        return f"[INCOMPLETE - hit {max_rounds}-round limit]\n{text}" if text else "(forced stop, no summary)"

    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def agent_loop(messages: list) -> str:
    rounds_since_todo = 0
    UI.new_tool_cycle()

    while True:
        if rounds_since_todo >= 8:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            UI.nag_reminder()

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )

        assistant_msg = {
            "role": "assistant",
            "content": [block.model_dump() for block in response.content],
        }
        messages.append(assistant_msg)

        has_tool_use = any(
            getattr(block, "type", None) == "tool_use" for block in response.content
        )
        if not has_tool_use:
            return "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )

        results = []
        used_todo = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "sub_agent":
                desc = block.input.get("description", "subtask")
                UI.task_start(desc, block.input["prompt"])
                output = run_subagent(block.input["prompt"])
            else:
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"

            UI.tool_call(block.name, output)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)[:50000],
            })
            if block.name == "todo":
                used_todo = True

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        messages.append({"role": "user", "content": results})


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    UI.welcome()
    history = []
    while True:
        try:
            query = UI.get_input()
        except (EOFError, KeyboardInterrupt):
            UI.console.print("\n[dim]Bye.[/dim]")
            break
        if query.strip().lower() in ("q", "exit", "quit", ""):
            UI.console.print("[dim]Bye.[/dim]")
            break
        history.append({"role": "user", "content": query})
        try:
            reply = agent_loop(history)
        except Exception as exc:
            UI.error(str(exc))
            continue
        UI.show_reply(reply)
