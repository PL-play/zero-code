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


class TUIAdapter:
    """Bridge between synchronous agent codebase and the Textual App events."""

    def __init__(self):
        self._current_todo = ""
        self._tool_buffer = []

    def set_app(self, app):
        """Link the Textual App instance so we can dispatch calls thread-safely."""
        self.app = app

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _safe_dispatch(self, method_name: str, *args, **kwargs):
        """Call a method on the Textual app safely from the agent thread."""
        if hasattr(self, "app"):
            method = getattr(self.app, method_name, None)
            if method:
                import threading
                import asyncio
                
                # If we are already in the main asyncio event loop (e.g. from slash commands directly),
                # just call the method directly. Otherwise, use call_from_thread.
                try:
                    loop = asyncio.get_running_loop()
                    is_async = True
                except RuntimeError:
                    is_async = False
                    
                if is_async and threading.current_thread() is threading.main_thread():
                    method(*args, **kwargs)
                else:
                    self.app.call_from_thread(method, *args, **kwargs)

    ##### Agent Lifecycle / Execution Logging #####
    def new_tool_cycle(self):
        self._tool_buffer = []

    def tool_call(self, name: str, output: str, is_sub: bool = False):
        ts = self._ts()
        prefix = "  sub" if is_sub else "    "
        out_preview = output.replace("\n", " ").strip()
        if len(out_preview) > 120:
            out_preview = out_preview[:117] + "..."
        entry = f"[{ts}] {prefix} {name}: {out_preview}"
        self._tool_buffer.append(entry)
        
        # Send a more detailed output to the TUI logs panel
        output_str = str(output)
        if len(output_str) > 2000:
            output_str = output_str[:2000] + "\n... (truncated)"
        detailed_entry = f"[{ts}] {prefix} {name}:\n{output_str}\n" + "-"*40

        self._safe_dispatch("agent_log", detailed_entry)
        self._safe_dispatch("set_status", f"Running: {name}...")

    def task_start(self, desc: str, prompt_preview: str):
        ts = self._ts()
        if len(prompt_preview) > 100:
            prompt_preview = prompt_preview[:97] + "..."
        entry = f"[{ts}]      sub_agent ({desc}): {prompt_preview}"
        self._tool_buffer.append(entry)
        self._safe_dispatch("agent_log", entry)
        self._safe_dispatch("set_status", f"Sub-agent: {desc}")

    def subagent_text(self, text: str):
        ts = self._ts()
        preview = text.replace("\n", " ").strip()
        if len(preview) > 120:
            preview = preview[:117] + "..."
        entry = f"[{ts}]   sub> {preview}"
        self._tool_buffer.append(entry)
        self._safe_dispatch("agent_log", entry)

    def subagent_limit(self, max_rounds: int):
        ts = self._ts()
        entry = f"[{ts}]   sub> [hit {max_rounds}-round limit, forcing summary]"
        self._tool_buffer.append(entry)
        self._safe_dispatch("agent_log", entry)

    ##### Status Info #####
    def update_todo(self, text: str):
        self._current_todo = text
        self._safe_dispatch("update_todos", text)

        # Also update usage while we're syncing state
        try:
            ctx = globals().get("CTX")
            if ctx is not None:
                self._safe_dispatch("update_usage", ctx.all_usage_summary())
        except Exception:
            pass

    def show_reply(self, text: str):
        # The main loop now grabs the final yield directly and appends it to chat,
        # but if we wanted to push it here, we'd do:
        # self._safe_dispatch("append_chat", text, "agent")
        self._safe_dispatch("set_status", "Idle")

    def nag_reminder(self):
        ts = self._ts()
        entry = f"[{ts}]      [reminder: update your todos]"
        self._tool_buffer.append(entry)
        self._safe_dispatch("agent_log", entry)

    ##### Stubs for legacy CLI UI code to avoid crashing agent.py #####
    @property
    def console(self):
        class PseudoConsole:
            def __init__(self, parent):
                self.parent = parent
            
            def print(self, *args, **kwargs):
                # When scripts use UI.console.print, we forward it to the rich log
                # We mainly write the first argument if it's a rich object
                if args:
                    if len(args) == 1 and kwargs.get('end', '\n') == '\r':
                        # Ignore those round 1/15 update loops that overwrite previous lines
                        self.parent._safe_dispatch("set_status", str(args[0]))
                    else:
                        self.parent._safe_dispatch("agent_log", args[0])
        return PseudoConsole(self)

    def get_input(self) -> str:
        # Not needed in TUI, should not be called
        return ""

    def welcome(self):
        pass

    def error(self, text: str):
        ts = self._ts()
        entry = f"[{ts}] error: {text}"
        self._safe_dispatch("agent_log", f"[bold red]{entry}[/bold red]")


# Initialize global UI Instance (Replacing ConsoleUI)
UI = TUIAdapter()


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

