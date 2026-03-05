import asyncio
import json
import threading
from typing import Any

from llm_client.interface import LLMRequest

from core.runtime import AGENT_DIR, MODEL, WORKDIR, client
from core.state import CTX, ContextManager, SKILL_LOADER, TODO, UI
from core.tools import CHILD_TOOLS, EXPLORE_TOOLS, PARENT_TOOLS, TOOL_HANDLERS

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
5. Delegate: use sub_agent(mode=\"explore\") for codebase exploration, sub_agent(mode=\"execute\") for independent subtasks. Keep the main agent focused on orchestration for large tasks.

# Tool Usage
- bash: persistent session — cwd and env vars survive across calls. Use restart=true to reset. Avoid dangerous commands (rm -rf /, sudo, etc.).
- read_file: returns numbered lines (\"  1|code\"). Use offset/limit for large files. Pass a directory path to list contents. Always read before editing.
- write_file: creates parent dirs automatically. Use for new files only; prefer edit_file or apply_patch for existing files.
- edit_file: str_replace (old_text→new_text). old_text must be unique — include more context if ambiguous. Set replace_all=true to replace every occurrence. You MUST read_file before editing. Best for small changes (<20 lines).
- apply_patch: apply a patch using @@ context lines for positioning and +/- for line changes. Only specify a few context lines to locate the edit — no need to repeat the entire old text. You MUST read_file before patching. Best for large edits, multi-location changes, or when old text is very long. Format: "@@ context_line\n-old_line\n+new_line".
- glob: find files by pattern (e.g. \"*.py\", \"**/*.ts\"). Prefer over `bash find`.
- grep: search file contents by regex. Prefer over `bash grep/rg`. Supports include filter.
- load_skill: load specialized knowledge before tackling unfamiliar domains. Check available skills first.
- sub_agent: delegate to a child agent with fresh context. Use mode=\"explore\" for read-only investigation, mode=\"execute\" for tasks that modify files.
- todo: track multi-step tasks. Keep exactly one item in_progress at a time.
- background_run: run a shell command asynchronously in a background worker.
- check_background: inspect status/output of background tasks by task_id or list all.

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
- You need to explore unfamiliar parts of the codebase (use mode=\"explore\").
Keep the main agent focused on planning, integration, and quality checks.

# Per-Turn Checklist
Before responding each turn, ask yourself:
- Have I updated my todo state?
- Is there independent work I should delegate via sub_agent?
- Am I reading before editing?
- Am I keeping changes minimal and focused?

# Skills
Agent home directory: {AGENT_DIR}
Skills and cache are stored under the agent home, NOT the workspace. Use load_skill(name) to load a skill by name — it reads from agent home directly.
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

MAX_AGENT_ROUNDS = 100
RESULT_MAX_CHARS = 50000


def _is_cancelled(stop_event: threading.Event | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    mapped = []
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            mapped.append(tool)
            continue
        params = tool.get("input_schema") or {"type": "object", "properties": {}}
        mapped.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", "unknown_tool"),
                    "description": tool.get("description", ""),
                    "parameters": params,
                },
            }
        )
    return mapped


def _truncate_result(output: str) -> str:
    s = str(output)
    if len(s) <= RESULT_MAX_CHARS:
        return s
    return s[: RESULT_MAX_CHARS - 50] + f"\n... (truncated, {len(s)} total chars)"


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    if isinstance(fn, dict):
        return str(fn.get("name") or "unknown")
    return str(tool_call.get("name") or "unknown") if isinstance(tool_call, dict) else "unknown"


def _tool_call_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(fn, dict):
        return {}
    args = fn.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _assistant_text(response) -> str:
    text = (getattr(response, "raw_text", "") or "").strip()
    if text:
        return text
    return (getattr(response, "content_text", "") or "").strip()


async def _run_subagent_async(
    prompt: str,
    mode: str = "execute",
    max_rounds: int = 30,
    stop_event: threading.Event | None = None,
) -> str:
    is_explore = mode == "explore"
    tools = EXPLORE_TOOLS if is_explore else CHILD_TOOLS
    sys_prompt = EXPLORE_SUBAGENT_SYSTEM if is_explore else SUBAGENT_SYSTEM
    label = prompt[:40].replace("\n", " ").strip()

    sub_ctx = ContextManager(role=f"sub:{label}")
    sub_messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    response = None
    hit_limit = False

    for round_idx in range(max_rounds):
        if _is_cancelled(stop_event):
            CTX.record_subagent(label, sub_ctx)
            return "[cancelled]"

        response = await client.complete(
            LLMRequest(
                messages=sub_messages,
                system_prompt=sys_prompt,
                tools=_to_openai_tools(tools),
                tool_choice="auto",
                max_tokens=8000,
                temperature=0,
            )
        )
        sub_ctx.update_usage(response)

        text = _assistant_text(response)
        for line in text.splitlines():
            if line.strip():
                UI.subagent_text(line)

        tool_calls = response.get_tool_calls()

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        sub_messages.append(assistant_msg)

        if not tool_calls:
            break

        for tool_call in tool_calls:
            if _is_cancelled(stop_event):
                CTX.record_subagent(label, sub_ctx)
                return "[cancelled]"

            tool_name = _tool_call_name(tool_call)
            tool_args = _tool_call_args(tool_call)
            handler = TOOL_HANDLERS.get(tool_name)
            try:
                output = handler(**tool_args) if handler else f"Unknown tool: {tool_name}"
            except Exception as e:
                output = f"Error: {e}"
            UI.tool_call(tool_name, output, is_sub=True, tool_input=tool_args)
            sub_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tool_call.get("id") or f"tool_{tool_name}_{round_idx}"),
                    "name": tool_name,
                    "content": str(output)[:50000],
                }
            )

        if sub_ctx.should_compact():
            UI.tool_call("compact", f"subagent auto-compact at round {round_idx+1}", is_sub=True)
            sub_messages = await sub_ctx.compact_async(sub_messages)
            sub_ctx.reset_usage()
    else:
        hit_limit = True

    if response is None:
        CTX.record_subagent(label, sub_ctx)
        return "(no summary)"

    if hit_limit:
        UI.subagent_limit(max_rounds)
        sub_messages.append(
            {
                "role": "user",
                "content": (
                    f"You have reached the maximum of {max_rounds} tool-call rounds and must stop now.\n"
                    "Please summarize:\n"
                    "1) What you have accomplished so far\n"
                    "2) Key findings from your tool calls\n"
                    "3) What remains unfinished or uncertain\n"
                    "Be concise. Note clearly that this task may NOT be fully completed."
                ),
            }
        )
        summary_response = await client.complete(
            LLMRequest(
                messages=sub_messages,
                system_prompt=sys_prompt,
                max_tokens=8000,
                temperature=0,
            )
        )
        sub_ctx.update_usage(summary_response)
        CTX.record_subagent(label, sub_ctx)
        text = _assistant_text(summary_response)
        return f"[INCOMPLETE - hit {max_rounds}-round limit]\n{text}" if text else "(forced stop, no summary)"

    CTX.record_subagent(label, sub_ctx)
    return _assistant_text(response) or "(no summary)"


def run_subagent(
    prompt: str,
    mode: str = "execute",
    max_rounds: int = 30,
    stop_event: threading.Event | None = None,
) -> str:
    return asyncio.run(_run_subagent_async(prompt=prompt, mode=mode, max_rounds=max_rounds, stop_event=stop_event))


async def _agent_loop_async(messages: list, stop_event: threading.Event | None = None) -> str:
    rounds_since_todo = 0
    UI.new_tool_cycle()

    for round_idx in range(MAX_AGENT_ROUNDS):
        if _is_cancelled(stop_event):
            return "[cancelled by user]"

        if rounds_since_todo >= 5 and TODO.has_in_progress:
            messages.append({"role": "user", "content": "<reminder>You have an in_progress todo. Update your todos.</reminder>"})
            UI.nag_reminder()
            rounds_since_todo = 0

        CTX.microcompact(messages)

        if CTX.should_compact():
            UI.console.print(f"[dim]{UI._ts()} [auto-compact triggered, saving transcript...][/dim]")
            messages[:] = await CTX.compact_async(messages)
            CTX.reset_usage()

        todo_snap = TODO.snapshot_for_prompt()
        if todo_snap:
            messages.append({"role": "user", "content": todo_snap})

        if hasattr(UI, "stream_start"):
            UI.stream_start()

        response = await client.complete(
            LLMRequest(
                messages=messages,
                system_prompt=SYSTEM,
                tools=_to_openai_tools(PARENT_TOOLS),
                tool_choice="auto",
                max_tokens=8000,
                temperature=0,
            ),
            on_chunk_delta_text=getattr(UI, "stream_text", None),
            on_chunk_think=getattr(UI, "stream_think", None),
        )

        if todo_snap:
            messages.pop()

        if hasattr(UI, "stream_end"):
            UI.stream_end()

        usage = CTX.update_usage(response)
        UI.console.print(
            f"[dim]{UI._ts()} round {round_idx+1}/{MAX_AGENT_ROUNDS} | "
            f"{usage['input']:,}in {usage['output']:,}out | session: {usage['total_in']:,}+{usage['total_out']:,}[/dim]",
            end="\r",
        )

        # Push usage to TUI after every round (not just on todo updates)
        if hasattr(UI, "_safe_dispatch"):
            UI._safe_dispatch("update_usage", CTX.all_usage_summary())

        text = _assistant_text(response)
        tool_calls = response.get_tool_calls()

        if hasattr(UI, "set_round_tools_present"):
            UI.set_round_tools_present(bool(tool_calls))

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            return text

        used_todo = False
        for tool_call in tool_calls:
            if _is_cancelled(stop_event):
                return "[cancelled by user]"

            tool_name = _tool_call_name(tool_call)
            tool_args = _tool_call_args(tool_call)

            if tool_name == "sub_agent":
                desc = tool_args.get("description", "subtask")
                sub_mode = tool_args.get("mode", "execute")
                prompt = tool_args.get("prompt", "")
                UI.task_start(desc, prompt)
                output = await _run_subagent_async(prompt, mode=sub_mode, stop_event=stop_event)
            else:
                handler = TOOL_HANDLERS.get(tool_name)
                try:
                    output = handler(**tool_args) if handler else f"Unknown tool: {tool_name}"
                except Exception as e:
                    output = f"Error: {e}"

            if tool_name == "read_file":
                CTX.track_file(tool_args.get("path", ""))

            UI.tool_call(tool_name, output, tool_input=tool_args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tool_call.get("id") or f"tool_{tool_name}_{round_idx}"),
                    "name": tool_name,
                    "content": _truncate_result(output),
                }
            )

            if tool_name == "todo":
                used_todo = True

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

    messages.append(
        {
            "role": "user",
            "content": (
                f"You have reached the maximum of {MAX_AGENT_ROUNDS} rounds. "
                "Summarize what you accomplished and what remains."
            ),
        }
    )
    response = await client.complete(
        LLMRequest(messages=messages, system_prompt=SYSTEM, max_tokens=8000, temperature=0)
    )
    CTX.update_usage(response)
    return f"[hit {MAX_AGENT_ROUNDS}-round limit]\n" + _assistant_text(response)


def agent_loop(messages: list, stop_event: threading.Event | None = None) -> str:
    return asyncio.run(_agent_loop_async(messages, stop_event=stop_event))
