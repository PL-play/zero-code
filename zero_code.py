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

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import yaml
from anthropic import Anthropic
from anthropic.types import MessageParam
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
        self._usage_area_height = 0
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
        total = self._tool_area_height + self._todo_area_height + self._usage_area_height
        self._clear_up(total)
        self._tool_area_height = 0
        self._todo_area_height = 0
        self._usage_area_height = 0

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

    def _render_usage(self):
        """Render token usage panel from global CTX."""
        try:
            ctx = globals().get("CTX")
            if ctx is None:
                self._usage_area_height = 0
                return
            text = ctx.all_usage_summary()
            panel = Panel(
                Text(text),
                title="[bold yellow]Token Usage[/bold yellow]",
                subtitle=f"[dim]{self._ts()}[/dim]",
                border_style="yellow",
                padding=(0, 1),
            )
            self._usage_area_height = self._measure(panel)
            self.console.print(panel)
        except Exception:
            self._usage_area_height = 0

    def _refresh(self):
        self._clear_dynamic()
        self._render_tool_panel()
        self._render_usage()
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
        self._clear_up(self._todo_area_height + self._usage_area_height)
        self._todo_area_height = 0
        self._usage_area_height = 0
        self._current_todo = text
        self._render_usage()
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
                title="[bold blue]ZERO-CODE[/bold blue] [red]by zhangran[red]",
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

    def snapshot_for_prompt(self) -> str:
        """Return a block to inject into the system prompt each turn."""
        if not self.items:
            return ""
        return f"\n<current_todos>\n{self.render()}\n</current_todos>"

    @property
    def has_in_progress(self) -> bool:
        return any(item["status"] == "in_progress" for item in self.items)


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

CACHE_DIR = WORKDIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

COMPACT_THRESHOLD = int(os.getenv("CONTEXT_COMPACT_THRESHOLD", "50000"))
MICRO_HOT_TAIL = 4  # keep last N tool_result messages fully inline
MICRO_SIZE_LIMIT = 1000  # offload tool outputs larger than this (chars)


# ---------------------------------------------------------------------------
# ContextManager — three-layer compaction
# ---------------------------------------------------------------------------

class ContextManager:
    """Microcompact + auto-compact + manual-compact with rehydration."""

    def __init__(self, role: str = "main"):
        self.role = role
        self.recent_files: list[str] = []
        self._file_counter = 0
        self.last_input_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.cache_read_tokens = 0
        self.api_calls = 0
        self._compact_count = 0
        self.subagent_records: list[dict] = []

    def track_file(self, path: str):
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        self.recent_files = self.recent_files[-5:]

    # -- token tracking -----------------------------------------------------

    def update_usage(self, response) -> dict:
        """Extract usage from API response and accumulate totals. Returns usage dict."""
        usage = response.usage
        self.last_input_tokens = getattr(usage, "input_tokens", 0)
        self.total_input_tokens += self.last_input_tokens
        self.total_output_tokens += getattr(usage, "output_tokens", 0)
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.api_calls += 1
        return {
            "input": self.last_input_tokens,
            "output": getattr(usage, "output_tokens", 0),
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "total_in": self.total_input_tokens,
            "total_out": self.total_output_tokens,
            "calls": self.api_calls,
        }

    def usage_summary(self) -> str:
        return (
            f"input={self.last_input_tokens:,} | "
            f"session: {self.total_input_tokens:,}in + {self.total_output_tokens:,}out | "
            f"cache_read={self.cache_read_tokens:,} | "
            f"calls={self.api_calls}"
        )

    def reset_usage(self):
        """Reset after compaction."""
        self.last_input_tokens = 0

    def record_subagent(self, label: str, sub_ctx: "ContextManager"):
        """Merge a subagent's usage into the global record."""
        rec = {
            "label": label,
            "input_tokens": sub_ctx.total_input_tokens,
            "output_tokens": sub_ctx.total_output_tokens,
            "cache_read": sub_ctx.cache_read_tokens,
            "api_calls": sub_ctx.api_calls,
            "compactions": getattr(sub_ctx, "_compact_count", 0),
        }
        self.subagent_records.append(rec)
        self.total_input_tokens += sub_ctx.total_input_tokens
        self.total_output_tokens += sub_ctx.total_output_tokens
        self.cache_read_tokens += sub_ctx.cache_read_tokens
        self.api_calls += sub_ctx.api_calls

    def all_usage_summary(self) -> str:
        """Full usage including subagent breakdown."""
        lines = [
            f"Main:  {self.total_input_tokens - sum(r['input_tokens'] for r in self.subagent_records):,}in "
            f"+ {self.total_output_tokens - sum(r['output_tokens'] for r in self.subagent_records):,}out "
            f"| calls={self.api_calls - sum(r['api_calls'] for r in self.subagent_records)}",
        ]
        for i, r in enumerate(self.subagent_records):
            lines.append(
                f"Sub#{i+1} ({r['label']}): {r['input_tokens']:,}in + {r['output_tokens']:,}out "
                f"| calls={r['api_calls']} compact={r['compactions']}"
            )
        lines.append(
            f"Total: {self.total_input_tokens:,}in + {self.total_output_tokens:,}out "
            f"| cache_read={self.cache_read_tokens:,} | calls={self.api_calls}"
        )
        return "\n".join(lines)

    # -- microcompaction ----------------------------------------------------

    def microcompact(self, messages: list):
        """Offload old large tool outputs to disk, keep hot tail inline."""
        tool_call_index = self._build_tool_call_index(messages)

        tool_result_indices = []
        for i, msg in enumerate(messages):
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content", "")
                        if isinstance(content, str) and len(content) > MICRO_SIZE_LIMIT:
                            tool_result_indices.append((i, block))

        if len(tool_result_indices) <= MICRO_HOT_TAIL:
            return

        cold = tool_result_indices[:-MICRO_HOT_TAIL]
        for _, block in cold:
            content = block["content"]
            if content.startswith("[tool output saved to"):
                continue
            self._file_counter += 1
            cache_path = CACHE_DIR / f"tool_{self._file_counter:05d}.md"

            tool_id = block.get("tool_use_id", "")
            call_info = tool_call_index.get(tool_id, {})
            tool_name = call_info.get("name", "unknown")
            tool_input = call_info.get("input", {})
            input_preview = json.dumps(tool_input, ensure_ascii=False, default=str)
            if len(input_preview) > 500:
                input_preview = input_preview[:497] + "..."

            md = (
                f"# Tool Call: {tool_name}\n\n"
                f"**ID**: `{tool_id}`\n\n"
                f"**Input**:\n```json\n{input_preview}\n```\n\n"
                f"**Output** ({len(content)} chars):\n\n"
                f"{content}\n"
            )
            cache_path.write_text(md)
            block["content"] = f"[tool output saved to {cache_path.relative_to(WORKDIR)}, {tool_name}, {len(content)} chars]"

    @staticmethod
    def _build_tool_call_index(messages: list) -> dict:
        """Build a mapping from tool_use_id -> {name, input} across all assistant messages."""
        index = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    index[block.get("id", "")] = {
                        "name": block.get("name", "unknown"),
                        "input": block.get("input", {}),
                    }
        return index

    # -- auto compaction ----------------------------------------------------

    def should_compact(self) -> bool:
        """Check if context is approaching limit based on actual API token usage."""
        return self.last_input_tokens > COMPACT_THRESHOLD

    def compact(self, messages: list, focus: str = None) -> list:
        """Summarize conversation and rehydrate with todo + recent files."""
        self._compact_count += 1
        transcript_path = CACHE_DIR / f"transcript_{int(time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
        focus_instruction = ""
        if focus:
            focus_instruction = f"\nFocus especially on: {focus}\n"

        compact_messages: list[MessageParam] = [{
            "role": "user",
            "content": (
                "Summarize this conversation for continuity. Include:\n"
                "1) What was accomplished\n"
                "2) Current state of the codebase and any in-progress work\n"
                "3) Key technical decisions made and why\n"
                "4) Open tasks and next steps\n"
                "5) Errors encountered and how they were resolved\n"
                "6) Files touched and why they matter\n"
                f"{focus_instruction}"
                "Be concise but preserve critical details needed to continue without re-asking.\n\n"
                + conversation_text
            ),
        }]
        summary_response = client.messages.create(
            model=MODEL,
            messages=compact_messages,
            max_tokens=8000,
        )
        summary = summary_response.content[0].text
        return self._rehydrate(summary, transcript_path)

    def _rehydrate(self, summary: str, transcript_path: Path) -> list:
        """Rebuild context after compaction: summary + todos + recent files."""
        parts = [
            "This session is being continued from a previous conversation that ran out of context.",
            f"Transcript saved to: {transcript_path.relative_to(WORKDIR)}",
            "",
            summary,
        ]

        todo_state = TODO.render()
        if todo_state and todo_state != "No todos.":
            parts.append(f"\n<current_todos>\n{todo_state}\n</current_todos>")

        if self.recent_files:
            parts.append(f"\nRecently accessed files: {', '.join(self.recent_files)}")

        parts.append("\nPlease continue from where we left off without asking the user any further questions.")

        return [
            {"role": "user", "content": "\n".join(parts)},
            {"role": "assistant", "content": "Understood. I have the context from the summary and will continue the task."},
        ]


CTX = ContextManager()


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM = f"""\
You are an interactive CLI coding agent working at {WORKDIR}.

# Identity
You assist the user with software engineering tasks: fixing bugs, adding features, refactoring, explaining code, and more.
Model: {MODEL}

# Tone and Style
- Output is displayed in a terminal. Be concise and direct; use markdown sparingly.
- Only use emojis if the user explicitly asks.
- Prioritize technical accuracy over validating the user's beliefs. Respectful correction is more valuable than false agreement.
- Never propose changes to code you haven't read. Always read first, then modify.
- Avoid over-engineering. Only make changes directly requested or clearly necessary.
  - Don't add features, refactoring, comments, or type annotations beyond what was asked.
  - Don't add error handling for scenarios that can't happen.
  - Don't create abstractions for one-time operations. Three similar lines are better than a premature abstraction.

# Workflow
1. Understand: read relevant files before acting. Use glob/grep to find files, not bash.
2. Plan: for multi-step tasks (3+ steps), create a todo list FIRST. Break complex tasks into concrete, actionable items.
3. Execute: work through items one at a time. Mark each in_progress before starting, completed immediately when done — never batch updates.
4. Verify: after edits, use bash to run tests or linters when appropriate. Check your work.
5. Delegate: use sub_agent(mode="explore") for codebase exploration, sub_agent(mode="execute") for independent subtasks. Keep the main agent focused on orchestration for large tasks.

# Tool Usage
- bash: persistent session — cwd and env vars survive across calls. Use restart=true to reset. Avoid dangerous commands (rm -rf /, sudo, etc.).
- read_file: returns numbered lines ("  1|code"). Use offset/limit for large files. Pass a directory path to list contents. Always read before editing.
- write_file: creates parent dirs automatically. Use for new files only; prefer edit_file for existing files.
- edit_file: str_replace (old_text→new_text) or insert (insert_line+insert_text). old_text must be unique in the file — include more surrounding context if ambiguous. Returns context around the change so you can verify.
- glob: find files by pattern (e.g. "*.py", "**/*.ts"). Prefer over `bash find`.
- grep: search file contents by regex. Prefer over `bash grep/rg`. Supports include filter.
- load_skill: load specialized knowledge before tackling unfamiliar domains. Check available skills first.
- sub_agent: delegate to a child agent with fresh context. Use mode="explore" for read-only investigation, mode="execute" for tasks that modify files.
- todo: track multi-step tasks. Keep exactly one item in_progress at a time.

# Todo Discipline
- Use the todo tool for any task with 3+ steps. This is mandatory, not optional.
- Mark a todo in_progress BEFORE you start working on it.
- Mark it completed IMMEDIATELY when done — do not wait to batch multiple completions.
- Only one item should be in_progress at any time.
- Review your todo list regularly to decide what to do next.

# Delegation Guidelines
Use sub_agent when:
- There are 2+ independent subtasks that don't depend on each other.
- Work involves cross-file investigation or comparison.
- You need to explore unfamiliar parts of the codebase (use mode="explore").
Keep the main agent focused on planning, integration, and quality checks.

# Per-Turn Checklist
Before responding each turn, ask yourself:
- Have I updated my todo state?
- Is there independent work I should delegate via sub_agent?
- Am I reading before editing?
- Am I keeping changes minimal and focused?

# Skills
{SKILL_LOADER.get_descriptions()}"""

SUBAGENT_SYSTEM = f"""\
You are a coding subagent at {WORKDIR}.
You have full read/write access to the workspace. Complete the given task thoroughly, then summarize:
1) What you accomplished
2) Key findings with specific file paths and line numbers
3) Any issues or uncertainties
Be concise and evidence-based."""

EXPLORE_SUBAGENT_SYSTEM = f"""\
You are a read-only exploration subagent at {WORKDIR}.
You can search and read files but CANNOT modify them. Your job is to investigate and report.
Use glob/grep to find files, read_file to examine them, load_skill for domain knowledge.
Return a concise summary with:
1) Key findings with specific file paths and line numbers
2) Relevant code snippets or patterns discovered
3) Any uncertainties or areas needing further investigation"""


# ---------------------------------------------------------------------------
# BashSession — persistent shell with state across commands
# ---------------------------------------------------------------------------

MAX_OUTPUT_LINES = 200
SENTINEL = "___ZERO_CODE_CMD_DONE___"


class BashSession:
    """Persistent bash process that keeps env vars and cwd across calls."""

    def __init__(self, cwd: Path):
        self._cwd = cwd
        self._proc = None
        self._start()

    def _start(self):
        self._proc = subprocess.Popen(
            ["/bin/bash", "--norc", "--noprofile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
            cwd=str(self._cwd),
        )
        self._stdout_q: queue.Queue[str] = queue.Queue()
        self._stderr_q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._reader, args=(self._proc.stdout, self._stdout_q), daemon=True).start()
        threading.Thread(target=self._reader, args=(self._proc.stderr, self._stderr_q), daemon=True).start()

    @staticmethod
    def _reader(stream, q: queue.Queue):
        for line in stream:
            q.put(line)

    def _drain(self, q: queue.Queue, timeout: float) -> tuple[list[str], str | None]:
        """Drain lines until sentinel. Returns (lines, exit_code_or_None)."""
        lines = []
        exit_code = None
        try:
            while True:
                line = q.get(timeout=timeout)
                if SENTINEL in line:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
                        exit_code = parts[-1]
                    break
                lines.append(line.rstrip("\n"))
        except queue.Empty:
            pass
        return lines, exit_code

    def execute(self, command: str, timeout: int = 120) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"

        if self._proc is None or self._proc.poll() is not None:
            self._start()

        full_cmd = f"{command}\necho {SENTINEL} $?\necho {SENTINEL} >&2\n"
        try:
            self._proc.stdin.write(full_cmd)
            self._proc.stdin.flush()
        except BrokenPipeError:
            self._start()
            return "Error: Bash session crashed, restarted. Please retry."

        stdout_lines, exit_code = self._drain(self._stdout_q, timeout)
        stderr_lines, _ = self._drain(self._stderr_q, timeout=0.5)

        if exit_code is None:
            exit_code = "?"

        parts = []
        if stdout_lines:
            if len(stdout_lines) > MAX_OUTPUT_LINES:
                kept = stdout_lines[-MAX_OUTPUT_LINES:]
                out = f"... ({len(stdout_lines) - MAX_OUTPUT_LINES} lines above) ...\n" + "\n".join(kept)
            else:
                out = "\n".join(stdout_lines)
            parts.append(f"stdout:\n{out}")
        if stderr_lines:
            parts.append(f"stderr:\n{chr(10).join(stderr_lines[-50:])}")
        if not parts:
            parts.append("(no output)")
        parts.insert(0, f"exit_code={exit_code}")
        return "\n".join(parts)[:50000]

    def restart(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        self._start()
        return "Bash session restarted."


BASH = BashSession(WORKDIR)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def run_bash(command: str = None, restart: bool = False) -> str:
    if restart:
        return BASH.restart()
    if not command:
        return "Error: command is required (or set restart=true)"
    return BASH.execute(command)


def run_read(path: str, offset: int = None, limit: int = None) -> str:
    try:
        fp = safe_path(path)
        if fp.is_dir():
            return _list_directory(fp)
        text = fp.read_text()
        all_lines = text.splitlines()
        total = len(all_lines)
        start = max(0, (offset or 1) - 1)
        end = min(total, start + limit) if limit else total
        selected = all_lines[start:end]
        numbered = [f"{start + i + 1:>6}|{line}" for i, line in enumerate(selected)]
        header = f"({total} lines total)"
        if start > 0 or end < total:
            header = f"(showing lines {start+1}-{end} of {total})"
        return header + "\n" + "\n".join(numbered)
    except Exception as e:
        return f"Error: {e}"


def _list_directory(dp: Path) -> str:
    entries = sorted(dp.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"Directory: {dp.relative_to(WORKDIR)}/"]
    for entry in entries[:100]:
        prefix = "d " if entry.is_dir() else "f "
        size = ""
        if entry.is_file():
            size = f" ({entry.stat().st_size} bytes)"
        lines.append(f"  {prefix}{entry.name}{size}")
    if len(entries) > 100:
        lines.append(f"  ... and {len(entries) - 100} more entries")
    return "\n".join(lines)


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        existed = fp.exists()
        old_size = fp.stat().st_size if existed else 0
        fp.write_text(content)
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if existed:
            return f"Wrote {len(content)} bytes ({line_count} lines) to {path} (overwritten, was {old_size} bytes)"
        return f"Wrote {len(content)} bytes ({line_count} lines) to {path} (new file)"
    except Exception as e:
        return f"Error: {e}"


def _fuzzy_find(content: str, old_text: str) -> tuple[int, int] | None:
    """Multi-level matching: exact -> stripped -> normalized whitespace."""
    idx = content.find(old_text)
    if idx != -1:
        return idx, idx + len(old_text)

    stripped = old_text.strip()
    for i, line in enumerate(content.splitlines(keepends=True)):
        if stripped in line.strip():
            break
    else:
        norm_content = re.sub(r"\s+", " ", content)
        norm_old = re.sub(r"\s+", " ", old_text.strip())
        pos = norm_content.find(norm_old)
        if pos == -1:
            return None
        char_count = 0
        real_start = 0
        for ci, ch in enumerate(content):
            if char_count == pos:
                real_start = ci
                break
            if ch.isspace():
                while char_count < len(norm_content) and norm_content[char_count] == " ":
                    char_count += 1
            else:
                char_count += 1
        real_end = min(real_start + len(old_text) + 50, len(content))
        chunk = content[real_start:real_end]
        norm_chunk = re.sub(r"\s+", " ", chunk)
        if norm_old in norm_chunk:
            return real_start, real_end
        return None

    return None


def _edit_context(lines: list[str], change_start: int, change_end: int, ctx: int = 3) -> str:
    """Return a few lines around the changed region with line numbers."""
    lo = max(0, change_start - ctx)
    hi = min(len(lines), change_end + ctx)
    numbered = [f"{lo + i + 1:>6}|{l}" for i, l in enumerate(lines[lo:hi])]
    return "\n".join(numbered)


def run_edit(
    path: str,
    old_text: str = None,
    new_text: str = None,
    insert_line: int = None,
    insert_text: str = None,
) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        lines = content.splitlines(keepends=True)

        if insert_line is not None and insert_text is not None:
            idx = max(0, min(insert_line, len(lines)))
            new_lines_to_insert = insert_text if insert_text.endswith("\n") else insert_text + "\n"
            lines.insert(idx, new_lines_to_insert)
            fp.write_text("".join(lines))
            result_lines = "".join(lines).splitlines()
            context = _edit_context(result_lines, idx, idx + insert_text.count("\n") + 1)
            return f"Inserted at line {idx} in {path}\n{context}"

        if old_text is None or new_text is None:
            return "Error: provide old_text+new_text for replacement, or insert_line+insert_text for insertion"

        count = content.count(old_text)
        if count == 0:
            match = _fuzzy_find(content, old_text)
            if match is None:
                return f"Error: Text not found in {path}. Provide a larger unique snippet."
            start, end = match
            updated = content[:start] + new_text + content[end:]
            fp.write_text(updated)
            result_lines = updated.splitlines()
            line_idx = content[:start].count("\n")
            context = _edit_context(result_lines, line_idx, line_idx + new_text.count("\n") + 1)
            return f"Edited {path} (fuzzy match)\n{context}"

        if count > 1:
            positions = []
            search_start = 0
            for _ in range(min(count, 5)):
                idx = content.find(old_text, search_start)
                if idx == -1:
                    break
                line_no = content[:idx].count("\n") + 1
                positions.append(str(line_no))
                search_start = idx + 1
            return (
                f"Error: old_text matches {count} locations in {path} (lines: {', '.join(positions)}). "
                "Provide more surrounding context to make it unique."
            )

        updated = content.replace(old_text, new_text, 1)
        fp.write_text(updated)
        result_lines = updated.splitlines()
        change_line = content.find(old_text)
        line_idx = content[:change_line].count("\n")
        context = _edit_context(result_lines, line_idx, line_idx + new_text.count("\n") + 1)
        return f"Edited {path}\n{context}"

    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, path: str = ".") -> str:
    try:
        base = safe_path(path)
        if not base.is_dir():
            return f"Error: {path} is not a directory"
        if not pattern.startswith("**/") and "/" not in pattern:
            pattern = "**/" + pattern
        matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return f"No files matching '{pattern}' in {path}"
        lines = [f"{m.relative_to(WORKDIR)}" for m in matches[:50]]
        result = "\n".join(lines)
        if len(matches) > 50:
            result += f"\n... and {len(matches) - 50} more"
        return result
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = ".", include: str = None, max_results: int = 50) -> str:
    try:
        base = safe_path(path)
        cmd = ["rg", "--no-heading", "--line-number", "--max-count", str(max_results), pattern, str(base)]
        if include:
            cmd.extend(["--glob", include])

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            # rg: 0=matched, 1=no matches, 2=error (invalid regex, etc.)
            if r.returncode > 1:
                err = r.stderr.strip() or "rg failed"
                return f"Error: {err}"
            out = r.stdout.strip()
            if not out:
                return f"No matches for '{pattern}'"
            lines = out.splitlines()[:max_results]
            return "\n".join(lines)
        except FileNotFoundError:
            # Fallback when ripgrep is unavailable.
            compiled = re.compile(pattern)
            results = []
            search_dir = base if base.is_dir() else base.parent
            glob_pat = include or "**/*"
            for fp in search_dir.glob(glob_pat):
                if not fp.is_file():
                    continue
                try:
                    for i, line in enumerate(fp.read_text().splitlines(), 1):
                        if compiled.search(line):
                            results.append(f"{fp.relative_to(WORKDIR)}:{i}:{line.rstrip()}")
                            if len(results) >= max_results:
                                break
                except (UnicodeDecodeError, PermissionError):
                    continue
                if len(results) >= max_results:
                    break
            return "\n".join(results) if results else f"No matches for '{pattern}'"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Dispatch map
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw.get("command"), kw.get("restart", False)),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("offset"), kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw.get("old_text"), kw.get("new_text"),
                                        kw.get("insert_line"), kw.get("insert_text")),
    "glob":       lambda **kw: run_glob(kw["pattern"], kw.get("path", ".")),
    "grep":       lambda **kw: run_grep(kw["pattern"], kw.get("path", "."),
                                        kw.get("include"), kw.get("max_results", 50)),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in a persistent bash session. State (cwd, env vars) persists across calls. Set restart=true to reset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "restart": {"type": "boolean", "description": "Set true to restart the bash session"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents with line numbers, or list directory entries. Supports offset/limit for partial reads.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "Start line number (1-indexed)"},
                "limit": {"type": "integer", "description": "Max number of lines to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates parent dirs). Reports overwrite if file existed.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file via str_replace (old_text->new_text) or insert at line number. Returns context around the change. Fails if old_text matches multiple locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Text to find and replace (must be unique)"},
                "new_text": {"type": "string", "description": "Replacement text"},
                "insert_line": {"type": "integer", "description": "Line number to insert after (0=start of file)"},
                "insert_text": {"type": "string", "description": "Text to insert"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern, sorted by modification time (newest first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py' or '**/*.ts'"},
                "path": {"type": "string", "description": "Directory to search in (default: workspace root)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents by regex pattern. Uses ripgrep if available, else Python re fallback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "File or directory to search (default: workspace root)"},
                "include": {"type": "string", "description": "Glob filter for filenames, e.g. '*.py'"},
                "max_results": {"type": "integer", "description": "Max matching lines to return (default 50)"},
            },
            "required": ["pattern"],
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
EXPLORE_TOOLS = [t for t in BASE_TOOLS if t["name"] not in ("write_file", "edit_file")]

PARENT_TOOLS = BASE_TOOLS + [
    {
        "name": "sub_agent",
        "description": "Spawn a subagent with fresh context. mode='explore' for read-only investigation, mode='execute' for tasks that modify files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string", "description": "Short label for logging"},
                "mode": {"type": "string", "enum": ["explore", "execute"], "description": "explore=read-only, execute=read-write (default: execute)"},
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

def run_subagent(prompt: str, mode: str = "execute", max_rounds: int = 30) -> str:
    is_explore = mode == "explore"
    tools = EXPLORE_TOOLS if is_explore else CHILD_TOOLS
    sys_prompt = EXPLORE_SUBAGENT_SYSTEM if is_explore else SUBAGENT_SYSTEM
    label = prompt[:40].replace("\n", " ").strip()

    sub_ctx = ContextManager(role=f"sub:{label}")
    sub_messages: list = [{"role": "user", "content": prompt}]
    response = None
    hit_limit = False

    for round_idx in range(max_rounds):
        response = client.messages.create(
            model=MODEL,
            system=sys_prompt,
            messages=sub_messages,
            tools=tools,
            max_tokens=8000,
        )
        sub_ctx.update_usage(response)

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

        if sub_ctx.should_compact():
            UI.tool_call("compact", f"subagent auto-compact at round {round_idx+1}", is_sub=True)
            sub_messages = sub_ctx.compact(sub_messages)
            sub_ctx.reset_usage()
    else:
        hit_limit = True

    if response is None:
        CTX.record_subagent(label, sub_ctx)
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
            system=sys_prompt,
            messages=sub_messages,
            tools=[],
            max_tokens=8000,
        )
        sub_ctx.update_usage(summary_response)
        CTX.record_subagent(label, sub_ctx)
        text = "".join(b.text for b in summary_response.content if hasattr(b, "text"))
        return f"[INCOMPLETE - hit {max_rounds}-round limit]\n{text}" if text else "(forced stop, no summary)"

    CTX.record_subagent(label, sub_ctx)
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

MAX_AGENT_ROUNDS = 100
RESULT_MAX_CHARS = 50000


def _truncate_result(output: str) -> str:
    s = str(output)
    if len(s) <= RESULT_MAX_CHARS:
        return s
    return s[:RESULT_MAX_CHARS - 50] + f"\n... (truncated, {len(s)} total chars)"


def agent_loop(messages: list) -> str:
    rounds_since_todo = 0
    UI.new_tool_cycle()

    for round_idx in range(MAX_AGENT_ROUNDS):
        # -- nag: only when todos exist with in_progress and no updates for 5 rounds
        if rounds_since_todo >= 5 and TODO.has_in_progress:
            messages.append({"role": "user", "content": "<reminder>You have an in_progress todo. Update your todos.</reminder>"})
            UI.nag_reminder()
            rounds_since_todo = 0

        # -- microcompaction: offload old large tool outputs
        CTX.microcompact(messages)

        # -- auto-compaction: compress if approaching context limit
        if CTX.should_compact():
            UI.console.print(f"[dim]{UI._ts()} [auto-compact triggered, saving transcript...][/dim]")
            messages[:] = CTX.compact(messages)
            CTX.reset_usage()

        # -- inject todo snapshot as ephemeral user message (cache-friendly)
        todo_snap = TODO.snapshot_for_prompt()
        if todo_snap:
            messages.append({"role": "user", "content": todo_snap})

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )

        # pop the ephemeral todo message before persisting assistant reply
        if todo_snap:
            messages.pop()

        # -- track actual token usage from API response
        usage = CTX.update_usage(response)
        UI.console.print(f"[dim]{UI._ts()} round {round_idx+1}/{MAX_AGENT_ROUNDS} | {usage['input']:,}in {usage['output']:,}out | session: {usage['total_in']:,}+{usage['total_out']:,}[/dim]", end="\r")

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
                sub_mode = block.input.get("mode", "execute")
                UI.task_start(desc, block.input["prompt"])
                output = run_subagent(block.input["prompt"], mode=sub_mode)
            else:
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"

            # track file access for rehydration
            if block.name == "read_file":
                CTX.track_file(block.input.get("path", ""))

            UI.tool_call(block.name, output)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _truncate_result(output),
            })
            if block.name == "todo":
                used_todo = True

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        messages.append({"role": "user", "content": results})

    # -- hit max rounds: force a summary
    messages.append({
        "role": "user",
        "content": (
            f"You have reached the maximum of {MAX_AGENT_ROUNDS} rounds. "
            "Summarize what you accomplished and what remains."
        ),
    })
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=messages,
        tools=[],
        max_tokens=8000,
    )
    CTX.update_usage(response)
    return f"[hit {MAX_AGENT_ROUNDS}-round limit]\n" + "\n".join(
        b.text for b in response.content if hasattr(b, "text")
    )


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

        stripped = query.strip().lower()
        if stripped in ("q", "exit", "quit", ""):
            UI.console.print("[dim]Bye.[/dim]")
            break

        # -- /compact command
        if stripped.startswith("/compact"):
            focus = query.strip()[len("/compact"):].strip() or None
            UI.console.print(f"[dim]{UI._ts()} Compacting conversation...[/dim]")
            history[:] = CTX.compact(history, focus=focus)
            CTX.reset_usage()
            UI.console.print(f"[dim]{UI._ts()} Done. Context compacted. {len(history)} messages remaining.[/dim]")
            continue

        # -- /context command: show usage
        if stripped == "/context":
            usage_text = CTX.all_usage_summary()
            panel = Panel(
                Text(f"{usage_text}\nmessages={len(history)} | compact_threshold={COMPACT_THRESHOLD:,}"),
                title="[bold yellow]Context & Token Usage[/bold yellow]",
                border_style="yellow",
                padding=(0, 1),
            )
            UI.console.print(panel)
            continue

        history.append({"role": "user", "content": query})
        try:
            reply = agent_loop(history)
        except Exception as exc:
            UI.error(str(exc))
            continue
        UI.show_reply(reply)
