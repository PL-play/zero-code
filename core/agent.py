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
- You need to explore unfamiliar parts of the codebase (use mode="explore").
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

        has_tool_use = any(getattr(block, "type", None) == "tool_use" for block in response.content)
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


def _truncate_result(output: str) -> str:
    s = str(output)
    if len(s) <= RESULT_MAX_CHARS:
        return s
    return s[:RESULT_MAX_CHARS - 50] + f"\n... (truncated, {len(s)} total chars)"


def agent_loop(messages: list) -> str:
    rounds_since_todo = 0
    UI.new_tool_cycle()

    for round_idx in range(MAX_AGENT_ROUNDS):
        if rounds_since_todo >= 5 and TODO.has_in_progress:
            messages.append({"role": "user", "content": "<reminder>You have an in_progress todo. Update your todos.</reminder>"})
            UI.nag_reminder()
            rounds_since_todo = 0

        CTX.microcompact(messages)

        if CTX.should_compact():
            UI.console.print(f"[dim]{UI._ts()} [auto-compact triggered, saving transcript...][/dim]")
            messages[:] = CTX.compact(messages)
            CTX.reset_usage()

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

        if todo_snap:
            messages.pop()

        usage = CTX.update_usage(response)
        UI.console.print(
            f"[dim]{UI._ts()} round {round_idx+1}/{MAX_AGENT_ROUNDS} | "
            f"{usage['input']:,}in {usage['output']:,}out | session: {usage['total_in']:,}+{usage['total_out']:,}[/dim]",
            end="\r",
        )

        assistant_msg = {
            "role": "assistant",
            "content": [block.model_dump() for block in response.content],
        }
        messages.append(assistant_msg)

        has_tool_use = any(getattr(block, "type", None) == "tool_use" for block in response.content)
        if not has_tool_use:
            return "\n".join(block.text for block in response.content if hasattr(block, "text"))

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
    return f"[hit {MAX_AGENT_ROUNDS}-round limit]\n" + "\n".join(b.text for b in response.content if hasattr(b, "text"))

