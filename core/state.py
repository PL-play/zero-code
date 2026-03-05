import json
import os
import re
import sys
import time
import asyncio
from datetime import datetime
from io import StringIO
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from llm_client.interface import LLMRequest

from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKDIR, client

TOOL_MAX_LINES = 20


class TUIAdapter:
    """Bridge between synchronous agent codebase and the Textual App events."""

    def __init__(self):
        self._current_todo = ""
        self._tool_buffer = []
        self._file_stats: dict[str, dict] = {}

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

    def _tool_brief(self, name: str, tool_input: dict | None, output: str) -> str:
        tool_input = tool_input or {}

        def _rel_path(p: str) -> str:
            if not p:
                return "?"
            try:
                raw = Path(p)
                resolved = raw.resolve() if raw.is_absolute() else (WORKDIR / raw).resolve()
                return str(resolved.relative_to(WORKDIR))
            except Exception:
                return p

        if name == "read_file":
            path = _rel_path(str(tool_input.get("path") or ""))
            lines = None
            m = re.search(r"\((\d+) lines total\)", str(output))
            if m:
                lines = m.group(1)
            else:
                m = re.search(r"\(showing lines \d+-\d+ of (\d+)\)", str(output))
                if m:
                    lines = m.group(1)
            suffix = f" ({lines} lines)" if lines else ""
            return f"read_file: {path}{suffix}"
        if name == "glob":
            pattern = str(tool_input.get("pattern") or "*")
            return f"glob: {pattern}"
        if name == "grep":
            pattern = str(tool_input.get("pattern") or "")
            return f"grep: {pattern}"
        if name == "bash":
            command = str(tool_input.get("command") or "").strip().replace("\n", " ")
            workdir_str = str(WORKDIR)
            if workdir_str in command:
                command = command.replace(workdir_str + "/", "")
                command = command.replace(workdir_str, ".")
            if len(command) > 60:
                command = command[:57] + "..."
            return f"bash: {command}" if command else "bash"
        if name == "edit_file":
            path = _rel_path(str(tool_input.get("path") or ""))
            replace_all = tool_input.get("replace_all", False)
            suffix = " (replace_all)" if replace_all else ""
            return f"edit_file: {path}{suffix}"
        if name == "apply_patch":
            path = _rel_path(str(tool_input.get("path") or ""))
            patch = str(tool_input.get("patch") or "")
            hunk_count = patch.count("@@")
            return f"apply_patch: {path} ({hunk_count} context markers)"
        if name == "write_file":
            path = _rel_path(str(tool_input.get("path") or ""))
            return f"write_file: {path}"
        preview = str(output).strip().splitlines()[0] if str(output).strip() else "(no output)"
        if len(preview) > 50:
            preview = preview[:47] + "..."
        return f"{name}: {preview}"

    def tool_call(self, name: str, output: str, is_sub: bool = False, tool_input: dict | None = None):
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
        if not is_sub:
            brief = self._tool_brief(name, tool_input, output)
            self._safe_dispatch("append_tool_brief", brief)
        self._safe_dispatch("set_status", f"Running: {name}...")

        # Send shell command outputs to the terminal tab
        if name in ("bash", "shell", "run_command"):
            cmd_str = ""
            if tool_input:
                cmd_str = tool_input.get("command", "")
            term_output = f"[bold #00FFCC]$[/bold #00FFCC] {cmd_str}\n{output_str}" if cmd_str else output_str
            self._safe_dispatch("terminal_log", term_output)

        if name in ("edit_file", "apply_patch") and not output.startswith("Error"):
            parts = output.split("\n", 1)
            summary = parts[0]
            diff_body = parts[1] if len(parts) > 1 else ""
            self._safe_dispatch("append_diff", summary, summary, diff_body)
            self._track_file_edit(tool_input, diff_body)

    def _track_file_edit(self, tool_input: dict | None, diff_body: str):
        tool_input = tool_input or {}
        raw_path = str(tool_input.get("path") or "")
        if not raw_path:
            return
        try:
            p = Path(raw_path)
            resolved = p.resolve() if p.is_absolute() else (WORKDIR / p).resolve()
            rel = str(resolved.relative_to(WORKDIR))
        except Exception:
            rel = raw_path

        added = 0
        deleted = 0
        for line in diff_body.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1

        if rel not in self._file_stats:
            self._file_stats[rel] = {"added": 0, "deleted": 0, "edits": 0}
        self._file_stats[rel]["added"] += added
        self._file_stats[rel]["deleted"] += deleted
        self._file_stats[rel]["edits"] += 1

        self._safe_dispatch("update_file_changes", self._render_file_stats())
        self._safe_dispatch("refresh_git_info")

    def _get_git_file_status(self, rel_path: str) -> str:
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", str(WORKDIR), "status", "--porcelain", "--", rel_path],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                raw = r.stdout.strip()[:2].strip() or "M"
                # Map git porcelain status to human-readable labels
                status_map = {
                    "??": "NEW",
                    "A": "NEW",
                    "M": "MOD",
                    "D": "DEL",
                    "R": "REN",
                    "C": "CPY",
                }
                return status_map.get(raw, raw)
            return " "
        except Exception:
            return "?"

    def _render_file_stats(self) -> str:
        if not self._file_stats:
            return "[bold #FACC15]Files Changed:[/bold #FACC15]\n  [dim](none)[/dim]"
        lines = ["[bold #FACC15]Files Changed:[/bold #FACC15]"]
        for path, stats in sorted(self._file_stats.items()):
            git_st = self._get_git_file_status(path)
            added = stats["added"]
            deleted = stats["deleted"]
            edits = stats["edits"]
            edit_word = "edit" if edits == 1 else "edits"
            # Color for git status
            if git_st == "NEW":
                st_tag = "[bold #55AAFF]NEW[/bold #55AAFF]"
            elif git_st == "DEL":
                st_tag = "[bold #FF5555]DEL[/bold #FF5555]"
            elif git_st == "MOD":
                st_tag = "[bold #FFAA44]MOD[/bold #FFAA44]"
            else:
                st_tag = f"[dim]{git_st}[/dim]"
            # Color for +/-
            add_str = f"[#88CC88]+{added}[/#88CC88]" if added else "[dim]+0[/dim]"
            del_str = f"[#FF8888]-{deleted}[/#FF8888]" if deleted else "[dim]-0[/dim]"
            lines.append(f"  {st_tag} {path}  {add_str} {del_str} [dim]({edits} {edit_word})[/dim]")
        return "\n".join(lines)

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

    def stream_start(self):
        self._safe_dispatch("stream_start")

    def stream_text(self, text: str):
        if text:
            self._safe_dispatch("append_stream_text", text)

    def stream_think(self, think: str):
        if think:
            self._safe_dispatch("append_stream_think", think)

    def stream_end(self):
        self._safe_dispatch("stream_end")

    def set_round_tools_present(self, has_tools: bool):
        self._safe_dispatch("set_round_tools_present", has_tools)

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

    def debug(self, text: str):
        ts = self._ts()
        entry = f"[{ts}] {text}"
        self._safe_dispatch("system_log", entry)


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
        self.reasoning_tokens = 0
        self.api_calls = 0
        self._compact_count = 0
        self.subagent_records: list[dict] = []

    def track_file(self, path: str):
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        self.recent_files = self.recent_files[-5:]

    def update_usage(self, response) -> dict:
        usage = None
        if hasattr(response, "token_usage") and response.token_usage is not None:
            usage = response.token_usage.as_dict()
        elif hasattr(response, "usage"):
            usage = response.usage

        def _pick(dct: dict, keys: list[str]) -> int:
            for key in keys:
                val = dct.get(key)
                if isinstance(val, (int, float)):
                    return int(val)
            return 0

        if isinstance(usage, dict):
            input_tokens = _pick(usage, ["prompt_tokens", "input_tokens"])
            output_tokens = _pick(usage, ["completion_tokens", "output_tokens"])
            cache_read = _pick(usage, ["cache_read_input_tokens", "cache_read_tokens",
                                       "cached_tokens", "cache_hit_tokens",
                                       "prompt_cached_tokens"])
            reasoning = _pick(usage, ["completion_reasoning_tokens", "reasoning_tokens"])
        else:
            input_tokens = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or getattr(usage, "cache_read_tokens", 0) or 0)
            reasoning = int(getattr(usage, "completion_reasoning_tokens", 0) or getattr(usage, "reasoning_tokens", 0) or 0)

        self.last_input_tokens = input_tokens
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.reasoning_tokens += reasoning
        self.api_calls += 1
        return {
            "input": self.last_input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
            "reasoning": reasoning,
            "total_in": self.total_input_tokens,
            "total_out": self.total_output_tokens,
            "total_reasoning": self.reasoning_tokens,
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
            "reasoning": sub_ctx.reasoning_tokens,
            "api_calls": sub_ctx.api_calls,
            "compactions": getattr(sub_ctx, "_compact_count", 0),
        }
        self.subagent_records.append(rec)
        self.total_input_tokens += sub_ctx.total_input_tokens
        self.total_output_tokens += sub_ctx.total_output_tokens
        self.cache_read_tokens += sub_ctx.cache_read_tokens
        self.reasoning_tokens += sub_ctx.reasoning_tokens
        self.api_calls += sub_ctx.api_calls

    def all_usage_summary(self) -> str:
        """Return Rich-markup formatted usage summary for the TUI Status panel."""
        main_in = self.total_input_tokens - sum(r['input_tokens'] for r in self.subagent_records)
        main_out = self.total_output_tokens - sum(r['output_tokens'] for r in self.subagent_records)
        main_reason = self.reasoning_tokens - sum(r.get('reasoning', 0) for r in self.subagent_records)
        main_calls = self.api_calls - sum(r['api_calls'] for r in self.subagent_records)

        lines = [
            "[bold #00FFCC]Token Usage[/bold #00FFCC]",
            "",
            "[bold #55AAFF]Main Agent[/bold #55AAFF]",
            f"  [#88CC88]IN[/#88CC88]  {main_in:,}  [#FF8888]OUT[/#FF8888]  {main_out:,}  [#AAAAAA]calls={main_calls}[/#AAAAAA]",
        ]
        if main_reason:
            lines[-1] += f"  [#D8B4FE]reason={main_reason:,}[/#D8B4FE]"

        for i, r in enumerate(self.subagent_records):
            lines.append(f"\n[bold #FACC15]Sub#{i+1}[/bold #FACC15] [dim]({r['label']})[/dim]")
            sub_line = f"  [#88CC88]IN[/#88CC88]  {r['input_tokens']:,}  [#FF8888]OUT[/#FF8888]  {r['output_tokens']:,}  [#AAAAAA]calls={r['api_calls']}[/#AAAAAA]"
            if r.get('reasoning'):
                sub_line += f"  [#D8B4FE]reason={r['reasoning']:,}[/#D8B4FE]"
            if r.get('compactions'):
                sub_line += f"  compact={r['compactions']}"
            lines.append(sub_line)

        lines.append("")
        lines.append("[bold]Session Total[/bold]")
        total_line = (
            f"  [bold #88CC88]IN[/bold #88CC88]  {self.total_input_tokens:,}  "
            f"[bold #FF8888]OUT[/bold #FF8888]  {self.total_output_tokens:,}  "
            f"[#FFAA44]cache={self.cache_read_tokens:,}[/#FFAA44]  "
            f"[#AAAAAA]calls={self.api_calls}[/#AAAAAA]"
        )
        if self.reasoning_tokens:
            total_line += f"  [bold #D8B4FE]reason={self.reasoning_tokens:,}[/bold #D8B4FE]"
        lines.append(total_line)
        return "\n".join(lines)

    def microcompact(self, messages: list):
        tool_call_index = self._build_tool_call_index(messages)

        tool_result_indices = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > MICRO_SIZE_LIMIT:
                    tool_result_indices.append((i, msg, None))
                continue
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content", "")
                        if isinstance(content, str) and len(content) > MICRO_SIZE_LIMIT:
                            tool_result_indices.append((i, msg, block))

        if len(tool_result_indices) <= MICRO_HOT_TAIL:
            return

        cold = tool_result_indices[:-MICRO_HOT_TAIL]
        for _, msg, block in cold:
            content = msg.get("content", "") if block is None else block.get("content", "")
            if content.startswith("[tool output saved to"):
                continue
            self._file_counter += 1
            cache_path = CACHE_DIR / f"tool_{self._file_counter:05d}.md"

            tool_id = msg.get("tool_call_id", "") if block is None else block.get("tool_use_id", "")
            call_info = tool_call_index.get(tool_id, {})
            tool_name = msg.get("name", "unknown") if block is None else call_info.get("name", "unknown")
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
            replaced = (
                f"[tool output saved to {cache_path.relative_to(AGENT_DIR)}, {tool_name}, {len(content)} chars]"
            )
            if block is None:
                msg["content"] = replaced
            else:
                block["content"] = replaced

    @staticmethod
    def _build_tool_call_index(messages: list) -> dict:
        index = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls", [])
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    index[str(tc.get("id", ""))] = {
                        "name": fn.get("name", tc.get("name", "unknown")),
                        "input": args,
                    }
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

    async def compact_async(self, messages: list, focus: str = None) -> list:
        self._compact_count += 1
        transcript_path = CACHE_DIR / f"transcript_{int(time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
        focus_instruction = ""
        if focus:
            focus_instruction = f"\nFocus especially on: {focus}\n"

        compact_messages = [{
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
        summary_response = await client.complete(
            LLMRequest(
                messages=compact_messages,
                model=MODEL,
                max_tokens=8000,
                temperature=0,
            )
        )
        summary = getattr(summary_response, "raw_text", "") or getattr(summary_response, "content_text", "")
        return self._rehydrate(summary, transcript_path)

    def compact(self, messages: list, focus: str = None) -> list:
        return asyncio.run(self.compact_async(messages, focus))

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

