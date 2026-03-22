import json
import os
import re
import time
import asyncio
from datetime import datetime
from pathlib import Path

import yaml
from llm_client.interface import LLMRequest

from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKSPACE_DIR, client
from core.agent_context import get_event_bus
from core.types import AgentEvent, AgentEventType
from core.ui.textual_adapter import TUIAdapter

TOOL_MAX_LINES = 20

# Global UI instance used by TUI and slash commands
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
        # Notify listeners (e.g. TUI) that todo state changed.
        get_event_bus().publish(
            AgentEvent(type=AgentEventType.TODO_UPDATED, payload={"text": result})
        )
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
        skill_path = Path(skill["path"]).resolve()
        skill_root = skill_path.parent
        return (
            f"<skill name=\"{name}\" path=\"{skill['path']}\">\n"
            f"Source: {skill['path']}\n\n"
            "[Execution Context]\n"
            f"- Workspace (user's project, default for file ops): {WORKSPACE_DIR}\n"
            f"- Agent home (zero-code installation): {AGENT_DIR}\n"
            f"- Skill root (this skill's scripts/assets): {skill_root}\n"
            "- Rules:\n"
            "  1) For relative commands in this skill (for example `python scripts/cli.py ...`), run from Skill root.\n"
            f"  2) Prefer absolute command form: `python {skill_root}/scripts/cli.py ...` when applicable.\n"
            "  3) Output files from skill execution should go to WORKSPACE, not skill root or agent home.\n"
            "  4) Do NOT search outside workspace/skill root unless user explicitly asks.\n\n"
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
                "6) Files touched and why they matter — use workspace-relative paths\n"
                "7) IMPORTANT: Preserve all file paths mentioned. Note that workspace is "
                f"{WORKSPACE_DIR} and agent home is {AGENT_DIR}. "
                "All user project files are in workspace.\n"
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
            "<path_reminder>",
            f"WORKSPACE (your working directory): {WORKSPACE_DIR}",
            f"AGENT HOME (zero-code installation): {AGENT_DIR}",
            f"SKILLS: {SKILLS_DIR}",
            "All relative file paths resolve to WORKSPACE. Files you created earlier in this session are in WORKSPACE.",
            "</path_reminder>",
            "",
            summary,
        ]

        todo_state = TODO.render()
        if todo_state and todo_state != "No todos.":
            parts.append(f"\n<current_todos>\n{todo_state}\n</current_todos>")

        if self.recent_files:
            parts.append(f"\nRecently accessed files (workspace-relative): {', '.join(self.recent_files)}")

        parts.append("\nPlease continue from where we left off without asking the user any further questions.")

        return [
            {"role": "user", "content": "\n".join(parts)},
            {"role": "assistant", "content": "Understood. I have the context from the summary and will continue the task."},
        ]


CTX = ContextManager()

