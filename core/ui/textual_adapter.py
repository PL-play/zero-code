"""Textual TUI bridge: dispatches to :class:`core.tui.ZeroCodeApp` from agent threads.

This module is intentionally separate from :mod:`core.state` so the state/memory
layer does not own UI code.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from core.runtime import AGENT_DIR, WORKSPACE_DIR
from core.types import AgentMessage
from core.ui_adapter import UIAdapter


def _format_path_for_ui(path: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(WORKSPACE_DIR):
        return str(resolved.relative_to(WORKSPACE_DIR))
    if resolved.is_relative_to(AGENT_DIR):
        return f"@agent/{resolved.relative_to(AGENT_DIR)}"
    return str(path)


class TUIAdapter(UIAdapter):
    """Bridge between synchronous agent codebase and the Textual App events."""

    def __init__(self):
        self._current_todo = ""
        self._tool_buffer: list[str] = []
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
                import asyncio
                import threading

                try:
                    asyncio.get_running_loop()
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
                resolved = raw.resolve() if raw.is_absolute() else (WORKSPACE_DIR / raw).resolve()
                return _format_path_for_ui(resolved)
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
            workdir_str = str(WORKSPACE_DIR)
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
        if name in ("generate_image", "edit_image"):
            try:
                payload = json.loads(str(output))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                if payload.get("ok") is True:
                    primary_path = payload.get("primary_path") or "(no file)"
                    image_count = payload.get("image_count") or 0
                    return f"{name}: ok, {image_count} image(s), {primary_path}"
                error = payload.get("error") or {}
                category = error.get("category") or "unknown_error"
                message = str(error.get("message") or "request failed")
                if len(message) > 60:
                    message = message[:57] + "..."
                return f"{name}: {category}, {message}"
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

        output_str = str(output)
        if len(output_str) > 2000:
            output_str = output_str[:2000] + "\n... (truncated)"
        detailed_entry = f"[{ts}] {prefix} {name}:\n{output_str}\n" + "-" * 40

        self._safe_dispatch("agent_log", detailed_entry)
        if not is_sub:
            brief = self._tool_brief(name, tool_input, output)
            self._safe_dispatch("append_tool_brief", brief)
            if name in ("generate_image", "edit_image"):
                try:
                    payload = json.loads(str(output))
                except Exception:
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is True:
                    paths = payload.get("paths") or []
                    if isinstance(paths, list) and paths:
                        self._safe_dispatch("set_pending_image_paths", [str(path) for path in paths], name)
        self._safe_dispatch("set_status", f"Running: {name}...")

        if name in ("edit_file", "apply_patch") and not output.startswith("Error"):
            parts = output.split("\n", 1)
            summary = parts[0]
            diff_body = parts[1] if len(parts) > 1 else ""
            self._safe_dispatch("append_diff", summary, summary, diff_body)
            self._track_file_edit(tool_input, diff_body)

        if name == "write_file" and not output.startswith("Error"):
            self._track_file_create(tool_input, output)

    def _track_file_edit(self, tool_input: dict | None, diff_body: str):
        tool_input = tool_input or {}
        raw_path = str(tool_input.get("path") or "")
        if not raw_path:
            return
        try:
            p = Path(raw_path)
            resolved = p.resolve() if p.is_absolute() else (WORKSPACE_DIR / p).resolve()
            rel = _format_path_for_ui(resolved)
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

    def _track_file_create(self, tool_input: dict | None, output: str):
        tool_input = tool_input or {}
        raw_path = str(tool_input.get("path") or "")
        if not raw_path:
            return
        try:
            p = Path(raw_path)
            resolved = p.resolve() if p.is_absolute() else (WORKSPACE_DIR / p).resolve()
            rel = _format_path_for_ui(resolved)
        except Exception:
            rel = raw_path

        content = str(tool_input.get("content") or "")
        added = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        if rel not in self._file_stats:
            self._file_stats[rel] = {"added": 0, "deleted": 0, "edits": 0}
        self._file_stats[rel]["added"] += added
        self._file_stats[rel]["edits"] += 1

        self._safe_dispatch("update_file_changes", self._render_file_stats())
        self._safe_dispatch("refresh_git_info")

    def _get_git_file_status(self, rel_path: str) -> str:
        if rel_path.startswith("@agent/"):
            return " "
        try:
            r = subprocess.run(
                ["git", "-C", str(WORKSPACE_DIR), "status", "--porcelain", "--", rel_path],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                raw = r.stdout.strip()[:2].strip() or "M"
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
            if git_st == "NEW":
                st_tag = "[bold #55AAFF]NEW[/bold #55AAFF]"
            elif git_st == "DEL":
                st_tag = "[bold #FF5555]DEL[/bold #FF5555]"
            elif git_st == "MOD":
                st_tag = "[bold #FFAA44]MOD[/bold #FFAA44]"
            else:
                st_tag = f"[dim]{git_st}[/dim]"
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

    def update_todo(self, text: str):
        self._current_todo = text
        self._safe_dispatch("update_todos", text)

        try:
            from core.state import CTX

            if CTX is not None:
                self._safe_dispatch("update_usage", CTX.all_usage_summary())
        except Exception:
            pass

    def show_reply(self, text: str):
        _ = text
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

    def nag_reminder(self, message: str | None = None):
        ts = self._ts()
        if not message:
            message = "update your todos"
        entry = f"[{ts}]      [reminder: {message}]"
        self._tool_buffer.append(entry)
        self._safe_dispatch("agent_log", entry)

    @property
    def console(self):
        class PseudoConsole:
            def __init__(self, parent):
                self.parent = parent

            def print(self, *args, **kwargs):
                if args:
                    if len(args) == 1 and kwargs.get("end", "\n") == "\r":
                        self.parent._safe_dispatch("set_status", str(args[0]))
                    else:
                        self.parent._safe_dispatch("agent_log", args[0])

        return PseudoConsole(self)

    def get_input(self) -> str:
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

    def show_message(self, message: AgentMessage, *, elapsed: float | None = None) -> None:
        role = message.role
        content = message.content
        style = "agent" if role == "assistant" else role
        self._safe_dispatch("append_chat", content, style, elapsed)

    def update_status(self, text: str) -> None:
        self._safe_dispatch("set_status", text)

    def log_agent(self, text: str) -> None:
        self._safe_dispatch("agent_log", text)

    def update_usage(self, usage_summary: str) -> None:
        self._safe_dispatch("update_usage", usage_summary)

    def show_tool_call_brief(self, name: str, brief: str) -> None:
        self._safe_dispatch("append_tool_brief", brief)

    def show_tool_call_detail(self, name: str, output: str, tool_input: dict | None = None) -> None:
        self.tool_call(name, output, is_sub=False, tool_input=tool_input or {})

    def handle_stream_delta(self, stream_id: str, text: str, *, is_think: bool) -> None:
        _ = stream_id
        if is_think:
            self.stream_think(text)
        else:
            self.stream_text(text)
