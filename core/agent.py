import asyncio
import json
import threading
from typing import Any

from llm_client.interface import LLMRequest

from core.runtime import AGENT_DIR, MODEL, SKILLS_DIR, WORKSPACE_DIR, client
from core.state import CTX, ContextManager, SKILL_LOADER, TODO, UI
from core.events import DEFAULT_EVENT_BUS
from core.types import AgentEvent, AgentEventType
from core.tools import CHILD_TOOLS, EXPLORE_TOOLS, PARENT_TOOLS, TOOL_HANDLERS

SYSTEM = f"""\
You are an interactive CLI coding agent working at {WORKSPACE_DIR}.

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

# Security & Sensitive Operations
- Never request the user's system password, sudo password, SSH passphrase, or any other secret (tokens, API keys, etc.) via chat.
- Do NOT simulate interactive password prompts like `Password:` in the chat UI. If a command would normally ask for a password (e.g. sudo, package manager requiring auth), instead:
  - Explain clearly what the user should run manually in their own terminal.
  - Do NOT ask them to paste the password back into this chat or into any tool.

# Tool Usage
CRITICAL — File path fidelity: When passing file paths to ANY tool, use the EXACT path string as given. NEVER rename, re-space, re-punctuate, or "beautify" file names. For example, if a file is named `foo-bar.md`, pass exactly `foo-bar.md` — do NOT change it to `foo - bar.md` or `foo_bar.md`. Copy-paste the path verbatim.
- bash: persistent session — cwd and env vars survive across calls. Use restart=true to reset. Avoid dangerous commands (rm -rf /, sudo, etc.). If a task requires sudo or other privileged commands, explain the exact commands for the user to run manually instead of trying to handle passwords yourself.
- read_file: returns numbered lines (\"  1|code\"). Use offset/limit for large files. Pass a directory path to list contents. Always read before editing.
- write_file: create or overwrite a file. Both "path" and "content" are REQUIRED and must be in ONE JSON object. Escape newlines as \\n in content. Use for new files; prefer edit_file or apply_patch for existing files.
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

# Paths — CRITICAL
- **YOUR WORKING DIRECTORY (workspace)**: {WORKSPACE_DIR}
  This is where the user's project lives. ALL file operations (read, write, edit, glob, grep, bash) default to this directory.
  When you generate files (images, code, etc.), they are saved HERE. When you need to find previously generated files, look HERE.
  Relative paths like "outputs/generated-images/foo.png" resolve to: {WORKSPACE_DIR}/outputs/generated-images/foo.png
- Agent home (zero-code installation): {AGENT_DIR}
  This is where the agent's own code and skills live. You almost never need to access this directly.
  Agent-home internals (core code, prompts) are restricted.
  Allowlisted agent paths that you CAN read/write: .cache, logs, and skills directory ({SKILLS_DIR}).
- Use @workspace/<path> or @agent/<path> when an explicit root is needed, but prefer plain relative paths (they default to workspace).
- IMPORTANT: Do NOT confuse workspace ({WORKSPACE_DIR}) with agent home ({AGENT_DIR}). They are different directories.
  If you generated a file at "outputs/foo.png", it is at {WORKSPACE_DIR}/outputs/foo.png, NOT at {AGENT_DIR}/outputs/foo.png.
- After load_skill(name), if that skill contains relative shell commands, execute them from that skill's root directory (or use absolute paths).

# Skills
Skills directory for this session is fixed at startup: {SKILLS_DIR}
This path will not change during the current process/session. It is re-resolved only when the app restarts.
Cache is stored under agent home. Use load_skill(name) to load a skill by name.
{SKILL_LOADER.get_descriptions()}"""

SUBAGENT_SYSTEM = f"""\
You are a coding subagent working in workspace: {WORKSPACE_DIR}
Agent home (do not confuse with workspace): {AGENT_DIR}

All file operations default to the workspace directory. Relative paths like "src/foo.py" mean {WORKSPACE_DIR}/src/foo.py.
Do NOT look for workspace files under agent home — they are different directories.

You have full read/write access to the workspace. Complete the given task thoroughly, then summarize:
1) What you accomplished
2) Key findings with specific file paths (workspace-relative) and line numbers
3) Any issues or uncertainties
Be concise and evidence-based."""

EXPLORE_SUBAGENT_SYSTEM = f"""\
You are a read-only exploration subagent working in workspace: {WORKSPACE_DIR}
Agent home (do not confuse with workspace): {AGENT_DIR}

All file operations default to the workspace directory. Relative paths like "src/foo.py" mean {WORKSPACE_DIR}/src/foo.py.
Do NOT look for workspace files under agent home — they are different directories.

You can search and read files but CANNOT modify them. Your job is to investigate and report.
Use glob/grep to find files, read_file to examine them, load_skill for domain knowledge.
Return a concise summary with:
1) Key findings with specific file paths (workspace-relative) and line numbers
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


DEBUG_ARG_MAX_CHARS = 300


def _debug_tool_call(tool_call: dict[str, Any]) -> None:
    """Log the raw tool call to the debug panel with per-param truncation."""
    name = _tool_call_name(tool_call)
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    raw_args = fn.get("arguments") if isinstance(fn, dict) else None

    # Parse for structured display
    parsed = None
    if isinstance(raw_args, dict):
        parsed = raw_args
    elif isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            pass

    lines = [f"tool_call: {name}"]
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            vs = str(v)
            if len(vs) > DEBUG_ARG_MAX_CHARS:
                vs = vs[:DEBUG_ARG_MAX_CHARS] + f"... ({len(vs)} chars total)"
            lines.append(f"  {k}: {vs}")
    else:
        # Raw string (possibly malformed)
        raw_str = str(raw_args) if raw_args is not None else "(none)"
        if len(raw_str) > DEBUG_ARG_MAX_CHARS * 2:
            raw_str = raw_str[:DEBUG_ARG_MAX_CHARS * 2] + f"... ({len(raw_str)} chars total)"
        lines.append(f"  raw_arguments: {raw_str}")
        lines.append("  ⚠ arguments is NOT valid JSON")

    try:
        UI._safe_dispatch("system_log", "\n".join(lines))
    except Exception:
        pass


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
            pass
        # Try to repair common JSON issues (unescaped newlines, trailing commas)
        try:
            repaired = args.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            parsed = json.loads(repaired)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    return {}


_REQUIRED_PARAMS: dict[str, list[str]] = {
    "write_file": ["path", "content"],
    "read_file": ["path"],
    "edit_file": ["path", "old_text", "new_text"],
    "apply_patch": ["path", "patch"],
    "bash": ["command"],
    "glob": ["pattern"],
    "grep": ["pattern"],
    "load_skill": ["name"],
    "todo": ["items"],
    "background_run": ["command"],
    "web_search": ["query"],
    "generate_image": ["prompt"],
    "edit_image": ["image_paths", "prompt"],
}


def _validate_tool_args(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """Return an error string if required params are missing, else None."""
    required = _REQUIRED_PARAMS.get(tool_name)
    if not required:
        return None
    missing = [p for p in required if p not in tool_args]
    if missing:
        return (
            f"Error: missing required parameter(s): {', '.join(missing)}. "
            f"{tool_name} requires: {', '.join(required)}. "
            f"Please provide all required parameters as a valid JSON object."
        )
    return None


def _sanitize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every tool call's function.arguments is valid JSON string.

    When the LLM produces malformed arguments, re-serialise them so the
    conversation history stays valid for the next API call.
    """
    sanitized = []
    for tc in tool_calls:
        tc = dict(tc)  # shallow copy
        fn = tc.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            args = fn.get("arguments")
            if isinstance(args, dict):
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                # Validate it's parseable JSON; if not, wrap as empty
                try:
                    json.loads(args)
                except Exception:
                    try:
                        repaired = args.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
                        json.loads(repaired)
                        fn["arguments"] = repaired
                    except Exception:
                        fn["arguments"] = "{}"
            elif args is None:
                fn["arguments"] = "{}"
            tc["function"] = fn
        sanitized.append(tc)
    return sanitized


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
            assistant_msg["tool_calls"] = _sanitize_tool_calls(tool_calls)
        sub_messages.append(assistant_msg)

        if not tool_calls:
            break

        for tool_call in tool_calls:
            if _is_cancelled(stop_event):
                CTX.record_subagent(label, sub_ctx)
                return "[cancelled]"

            _debug_tool_call(tool_call)
            tool_name = _tool_call_name(tool_call)
            tool_args = _tool_call_args(tool_call)

            validation_err = _validate_tool_args(tool_name, tool_args)
            if validation_err:
                output = validation_err
            else:
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
    DEFAULT_EVENT_BUS.publish(AgentEvent(type=AgentEventType.SESSION_STARTED, payload={}))

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
            DEFAULT_EVENT_BUS.publish(
                AgentEvent(
                    type=AgentEventType.STATUS_CHANGED,
                    payload={"status": "auto-compact triggered, saving transcript..."},
                )
            )
            messages[:] = await CTX.compact_async(messages)
            CTX.reset_usage()

        todo_snap = TODO.snapshot_for_prompt()
        if todo_snap:
            messages.append({"role": "user", "content": todo_snap})

        # Start a new response stream for this round
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
            # Route streaming chunks through UI adapter, which now also
            # emits structured AgentEvent STREAM_DELTA / STREAM_THINK_DELTA.
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
        DEFAULT_EVENT_BUS.publish(
            AgentEvent(
                type=AgentEventType.USAGE_UPDATED,
                payload={"usage": usage},
            )
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
            assistant_msg["tool_calls"] = _sanitize_tool_calls(tool_calls)
        messages.append(assistant_msg)

        if not tool_calls:
            return text

        used_todo = False
        for tool_call in tool_calls:
            if _is_cancelled(stop_event):
                return "[cancelled by user]"

            _debug_tool_call(tool_call)
            tool_name = _tool_call_name(tool_call)
            tool_args = _tool_call_args(tool_call)

            validation_err = _validate_tool_args(tool_name, tool_args)
            if validation_err:
                output = validation_err
            elif tool_name == "sub_agent":
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
    DEFAULT_EVENT_BUS.publish(
        AgentEvent(
            type=AgentEventType.SESSION_ENDED,
            payload={"reason": "hit_round_limit"},
        )
    )
    return f"[hit {MAX_AGENT_ROUNDS}-round limit]\n" + _assistant_text(response)


def agent_loop(messages: list, stop_event: threading.Event | None = None) -> str:
    return asyncio.run(_agent_loop_async(messages, stop_event=stop_event))
