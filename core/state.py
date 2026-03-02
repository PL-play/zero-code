import json
import os
import re
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import yaml
from anthropic.types import MessageParam
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKDIR, client

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

    def update_todo(self, text: str):
        self._clear_up(self._todo_area_height + self._usage_area_height)
        self._todo_area_height = 0
        self._usage_area_height = 0
        self._current_todo = text
        self._render_usage()
        self._render_todo()

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
        table.add_row("Agent Home", str(AGENT_DIR))
        table.add_row("Model", MODEL)
        self.console.print(
            Panel(
                table,
                title="[bold blue]ZERO-CODE[/bold blue] [red]by Ran[red]",
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
        if not self.items:
            return ""
        return f"\n<current_todos>\n{self.render()}\n</current_todos>"

    @property
    def has_in_progress(self) -> bool:
        return any(item["status"] == "in_progress" for item in self.items)


TODO = TodoManager()


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
            line = f"  - {name}: {desc} (path: {skill['path']})"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return (
            f"<skill name=\"{name}\" path=\"{skill['path']}\">\n"
            f"Source: {skill['path']}\n\n"
            f"{skill['body']}\n"
            f"</skill>"
        )


SKILL_LOADER = SkillLoader(SKILLS_DIR)

CACHE_DIR = AGENT_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

COMPACT_THRESHOLD = int(os.getenv("CONTEXT_COMPACT_THRESHOLD", "50000"))
MICRO_HOT_TAIL = 10
MICRO_SIZE_LIMIT = 1000


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

    def update_usage(self, response) -> dict:
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
        self.last_input_tokens = 0

    def record_subagent(self, label: str, sub_ctx: "ContextManager"):
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

    def microcompact(self, messages: list):
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
            block["content"] = (
                f"[tool output saved to {cache_path.relative_to(AGENT_DIR)}, {tool_name}, {len(content)} chars]"
            )

    @staticmethod
    def _build_tool_call_index(messages: list) -> dict:
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

    def should_compact(self) -> bool:
        return self.last_input_tokens > COMPACT_THRESHOLD

    def compact(self, messages: list, focus: str = None) -> list:
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
        parts = [
            "This session is being continued from a previous conversation that ran out of context.",
            f"Transcript saved to: {transcript_path.relative_to(AGENT_DIR)}",
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

