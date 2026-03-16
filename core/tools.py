import difflib
import json
import queue
import re
import subprocess
import threading
import uuid
from pathlib import Path

from core.runtime import AGENT_DIR, AGENT_RW_ALLOWLIST, IMAGE_EDIT_CONFIG, IMAGE_GENERATION_CONFIG, WEB_SEARCH_CONFIG, WORKSPACE_DIR, safe_path
from core.state import SKILL_LOADER, TODO
from llm_client.qwen_image import (
    QwenImageError,
    edit_image_with_qwen,
    generate_image_with_qwen,
    summarize_image_operation_error,
    summarize_image_operation_result,
)
from llm_client.web_search import (
    WebSearchError,
    search_web,
    summarize_search_error,
    summarize_search_result,
)

MAX_OUTPUT_LINES = 200
SENTINEL = "___ZERO_CODE_CMD_DONE___"

FILE_READ_STATE: dict[str, float] = {}


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


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(WORKSPACE_DIR):
        return str(resolved.relative_to(WORKSPACE_DIR))
    if resolved.is_relative_to(AGENT_DIR):
        return f"@agent/{resolved.relative_to(AGENT_DIR)}"
    return str(resolved)


BASH = BashSession(WORKSPACE_DIR)


class BackgroundManager:
    """Background command runner with task tracking."""

    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"

        scope_err = _validate_bash_command_scope(command)
        if scope_err:
            return scope_err

        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(target=self._execute, args=(task_id, command), daemon=True)
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=WORKSPACE_DIR,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"

        with self._lock:
            task = self.tasks.get(task_id)
            if task is not None:
                task["status"] = status
                task["result"] = output or "(no output)"

    def check(self, task_id: str = None) -> str:
        if task_id:
            task = self.tasks.get(task_id)
            if not task:
                return f"Error: Unknown task {task_id}"
            return f"[{task['status']}] {task['command'][:60]}\n{task.get('result') or '(running)'}"

        lines = []
        for tid, task in self.tasks.items():
            lines.append(f"{tid}: [{task['status']}] {task['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."


BG = BackgroundManager()


def _tool_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


_BASH_WRITE_HINTS = (
    " >",
    ">>",
    " tee ",
    " sed -i",
    " rm ",
    " mv ",
    " cp ",
    " touch ",
    " mkdir ",
)
_BASH_READ_HINTS = (
    " cat ",
    " less ",
    " head ",
    " tail ",
    " grep ",
    " rg ",
    " find ",
)


def _is_agent_path_allowed_for_bash(command: str) -> bool:
    lowered = command.lower()
    if str(AGENT_DIR).lower() not in lowered:
        return True
    return any(str(path).lower() in lowered for path in AGENT_RW_ALLOWLIST)


def _validate_bash_command_scope(command: str) -> str | None:
    lowered = f" {command.lower()} "
    if str(AGENT_DIR).lower() not in lowered:
        return None
    if _is_agent_path_allowed_for_bash(command):
        return None

    if any(hint in lowered for hint in _BASH_WRITE_HINTS):
        return (
            "Error: Writing under agent home is blocked outside allowlisted paths (.cache/logs). "
            "Use workspace files or allowlisted agent paths only."
        )
    if any(hint in lowered for hint in _BASH_READ_HINTS):
        return (
            "Error: Reading agent-home internals is blocked outside allowlisted paths (.cache/logs). "
            "Use load_skill(name) for skill content instead of direct file reads."
        )
    return None


def run_generate_image(
    prompt: str,
    negative_prompt: str | None = None,
    size: str | None = None,
    prompt_extend: bool | None = None,
    watermark: bool | None = None,
    output_dir: str | None = None,
    filename_prefix: str | None = None,
) -> str:
    if IMAGE_GENERATION_CONFIG is None:
        return _tool_json(
            summarize_image_operation_error(
                QwenImageError(
                    "generate_image tool is not configured. Set DASHSCOPE_API_KEY and DASHSCOPE_IMAGE_MODEL.",
                    category="configuration_error",
                ),
                operation="generate_image",
            )
        )

    try:
        resolved_output_dir = safe_path(output_dir or IMAGE_GENERATION_CONFIG.output_dir)
        result = generate_image_with_qwen(
            IMAGE_GENERATION_CONFIG,
            prompt=prompt,
            negative_prompt=negative_prompt,
            size=size,
            prompt_extend=prompt_extend,
            watermark=watermark,
            output_dir=resolved_output_dir,
            filename_prefix=filename_prefix,
            workspace_root=WORKSPACE_DIR,
        )
        summary = summarize_image_operation_result(result, operation="generate_image")
        summary["workspace"] = str(WORKSPACE_DIR)
        summary["note"] = "All paths are relative to workspace directory. Use these relative paths directly with read_file or other tools."
        return _tool_json(summary)
    except Exception as e:
        return _tool_json(summarize_image_operation_error(e, operation="generate_image"))


def run_edit_image(
    image_paths: list[str],
    prompt: str,
    negative_prompt: str | None = None,
    size: str | None = None,
    n: int | None = None,
    prompt_extend: bool | None = None,
    watermark: bool | None = None,
    output_dir: str | None = None,
    filename_prefix: str | None = None,
) -> str:
    if IMAGE_EDIT_CONFIG is None:
        return _tool_json(
            summarize_image_operation_error(
                QwenImageError(
                    "edit_image tool is not configured. Set DASHSCOPE_IMAGE_EDIT_MODEL and DASHSCOPE_API_KEY.",
                    category="configuration_error",
                ),
                operation="edit_image",
            )
        )

    resolved_image_paths: list[Path] = []
    try:
        resolved_output_dir = safe_path(output_dir or IMAGE_EDIT_CONFIG.output_dir)
        resolved_image_paths = [safe_path(path) for path in image_paths]
        result = edit_image_with_qwen(
            IMAGE_EDIT_CONFIG,
            prompt=prompt,
            image_paths=resolved_image_paths,
            negative_prompt=negative_prompt,
            size=size,
            n=n,
            prompt_extend=prompt_extend,
            watermark=watermark,
            output_dir=resolved_output_dir,
            filename_prefix=filename_prefix,
            workspace_root=WORKSPACE_DIR,
        )
        summary = summarize_image_operation_result(
            result,
            operation="edit_image",
            input_paths=[path.as_posix() for path in resolved_image_paths],
        )
        summary["workspace"] = str(WORKSPACE_DIR)
        summary["note"] = "All paths are relative to workspace directory. Use these relative paths directly with read_file or other tools."
        return _tool_json(summary)
    except Exception as e:
        return _tool_json(
            summarize_image_operation_error(
                e,
                operation="edit_image",
                input_paths=[path.as_posix() for path in resolved_image_paths] if resolved_image_paths else image_paths,
            )
        )


def _optional_generate_image_tools() -> list[dict[str, object]]:
    if IMAGE_GENERATION_CONFIG is None:
        return []

    return [
        {
            "name": "generate_image",
            "description": (
                "Generate an image with Qwen-Image via DashScope and save it into the workspace. "
                "Returns compact JSON with ok, image_count, primary_path, paths, request_id, and provider/model metadata."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Positive prompt describing the image to generate"},
                    "negative_prompt": {"type": "string", "description": "Optional negative prompt"},
                    "size": {"type": "string", "description": "Optional size like 1024*1024 or 1664*928"},
                    "prompt_extend": {"type": "boolean", "description": "Override whether the provider should auto-extend the prompt"},
                    "watermark": {"type": "boolean", "description": "Override whether the provider should add a watermark"},
                    "output_dir": {"type": "string", "description": "Optional workspace-relative directory to save generated images"},
                    "filename_prefix": {"type": "string", "description": "Optional file name prefix for generated images"},
                },
                "required": ["prompt"],
            },
        }
    ]


def _optional_edit_image_tools() -> list[dict[str, object]]:
    if IMAGE_EDIT_CONFIG is None:
        return []

    return [
        {
            "name": "edit_image",
            "description": (
                "Edit one to three local images with Qwen-Image via DashScope and save the edited images into the workspace. "
                "Returns compact JSON with ok, image_count, primary_path, input_paths, output paths, and provider/model metadata."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 3,
                        "description": "One to three workspace-relative image paths used as edit inputs",
                    },
                    "prompt": {"type": "string", "description": "Edit instruction describing the target image result"},
                    "negative_prompt": {"type": "string", "description": "Optional negative prompt"},
                    "size": {"type": "string", "description": "Optional output size like 1024*1536"},
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 6,
                        "description": "Optional number of output images (1-6 for supported models)",
                    },
                    "prompt_extend": {"type": "boolean", "description": "Override whether the provider should auto-extend the prompt"},
                    "watermark": {"type": "boolean", "description": "Override whether the provider should add a watermark"},
                    "output_dir": {"type": "string", "description": "Optional workspace-relative directory to save edited images"},
                    "filename_prefix": {"type": "string", "description": "Optional file name prefix for edited images"},
                },
                "required": ["image_paths", "prompt"],
            },
        }
    ]


def run_web_search(
    query: str,
    max_results: int | None = None,
    language: str | None = None,
    categories: str | None = None,
) -> str:
    try:
        data = search_web(
            WEB_SEARCH_CONFIG,
            query,
            max_results=max_results,
            language=language,
            categories=categories,
        )
        return _tool_json(summarize_search_result(data))
    except WebSearchError as e:
        return _tool_json(summarize_search_error(e))
    except Exception as e:
        return _tool_json({"status": "error", "error": str(e)})


def _optional_web_search_tools() -> list[dict[str, object]]:
    if WEB_SEARCH_CONFIG is None:
        return []

    return [
        {
            "name": "web_search",
            "description": (
                "Search the web using SearXNG. Returns ranked results with title, URL, and snippet. "
                "Use this to find current information, documentation, or answers from the internet."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of results to return (default: 5)",
                    },
                    "language": {"type": "string", "description": "Language code, e.g. 'en', 'zh'"},
                    "categories": {"type": "string", "description": "Comma-separated categories, e.g. 'general', 'news', 'science'"},
                },
                "required": ["query"],
            },
        }
    ]


def run_bash(command: str = None, restart: bool = False) -> str:
    if restart:
        return BASH.restart()
    if not command:
        return "Error: command is required (or set restart=true)"
    scope_err = _validate_bash_command_scope(command)
    if scope_err:
        return scope_err
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
        FILE_READ_STATE[str(fp)] = fp.stat().st_mtime
        return header + "\n" + "\n".join(numbered)
    except Exception as e:
        return f"Error: {e}"


def _list_directory(dp: Path) -> str:
    entries = sorted(dp.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"Directory: {_display_path(dp)}/"]
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
        display = _display_path(fp)
        if existed:
            return f"Wrote {len(content)} bytes ({line_count} lines) to {display} (overwritten, was {old_size} bytes) [workspace: {WORKSPACE_DIR}]"
        return f"Wrote {len(content)} bytes ({line_count} lines) to {display} (new file) [workspace: {WORKSPACE_DIR}]"
    except Exception as e:
        return f"Error: {e}"


def _check_read_state(fp: Path) -> str | None:
    key = str(fp)
    if key not in FILE_READ_STATE:
        return f"Error: File has not been read yet. Use read_file first before editing: {fp.name}"
    if fp.exists():
        current_mtime = fp.stat().st_mtime
        if current_mtime > FILE_READ_STATE[key]:
            return f"Error: File was modified since last read. Re-read it first: {fp.name}"
    return None


def _detect_line_ending(content: str) -> str:
    crlf_idx = content.find("\r\n")
    lf_idx = content.find("\n")
    if lf_idx == -1:
        return "\n"
    if crlf_idx == -1:
        return "\n"
    return "\r\n" if crlf_idx < lf_idx else "\n"


def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def _strip_bom(content: str) -> tuple[str, str]:
    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


def _normalize_unicode(text: str) -> str:
    lines = text.split("\n")
    stripped = "\n".join(line.rstrip() for line in lines)
    result = re.sub(r"[\u2018\u2019\u201a\u201b]", "'", stripped)
    result = re.sub(r"[\u201c\u201d\u201e\u201f]", '"', result)
    result = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", result)
    result = re.sub(r"[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", result)
    return result


def _fuzzy_find(content: str, old_text: str) -> tuple[int, int, str] | None:
    """4-level matching: exact -> CRLF-normalized -> Unicode-normalized -> line-trimmed.
    Returns (start, end, content_for_replacement) or None.
    """
    idx = content.find(old_text)
    if idx != -1:
        return idx, idx + len(old_text), content

    lf_content = _normalize_to_lf(content)
    lf_old = _normalize_to_lf(old_text)
    idx = lf_content.find(lf_old)
    if idx != -1:
        return idx, idx + len(lf_old), lf_content

    uni_content = _normalize_unicode(lf_content)
    uni_old = _normalize_unicode(lf_old)
    idx = uni_content.find(uni_old)
    if idx != -1:
        return idx, idx + len(uni_old), uni_content

    trim_content = "\n".join(line.strip() for line in lf_content.split("\n"))
    trim_old = "\n".join(line.strip() for line in lf_old.split("\n"))
    idx = trim_content.find(trim_old)
    if idx != -1:
        return idx, idx + len(trim_old), trim_content

    return None


def _generate_diff(old_content: str, new_content: str, context: int = 3) -> str:
    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="", n=context)
    return "\n".join(diff)


def run_edit(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    try:
        fp = safe_path(path)

        read_err = _check_read_state(fp)
        if read_err:
            return read_err

        raw_content = fp.read_text()
        bom, content = _strip_bom(raw_content)
        original_ending = _detect_line_ending(content)
        normalized = _normalize_to_lf(content)
        norm_old = _normalize_to_lf(old_text)
        norm_new = _normalize_to_lf(new_text)

        if norm_old == norm_new:
            return "Error: old_text and new_text are identical."

        if replace_all:
            match = _fuzzy_find(normalized, norm_old)
            if match is None:
                return f"Error: Text not found in {path}. Provide a larger unique snippet."
            _, _, base = match
            if base == normalized:
                count = base.count(norm_old)
                updated = base.replace(norm_old, norm_new)
            else:
                search_key = _normalize_unicode(norm_old)
                count = base.count(search_key)
                updated = base.replace(search_key, norm_new)
        else:
            match = _fuzzy_find(normalized, norm_old)
            if match is None:
                return f"Error: Text not found in {path}. Provide a larger unique snippet."
            start, end, base = match

            fuzzy_base = _normalize_unicode(_normalize_to_lf(base))
            fuzzy_old = _normalize_unicode(norm_old)
            occurrence_count = fuzzy_base.split(fuzzy_old)
            count = len(occurrence_count) - 1
            if count > 1:
                positions = []
                search_start = 0
                for _ in range(min(count, 5)):
                    idx = fuzzy_base.find(fuzzy_old, search_start)
                    if idx == -1:
                        break
                    line_no = fuzzy_base[:idx].count("\n") + 1
                    positions.append(str(line_no))
                    search_start = idx + 1
                return (
                    f"Error: old_text matches {count} locations in {path} (lines: {', '.join(positions)}). "
                    "Provide more surrounding context to make it unique, or use replace_all=true."
                )

            updated = base[:start] + norm_new + base[end:]
            count = 1

        diff_output = _generate_diff(base, updated)
        final = bom + _restore_line_endings(updated, original_ending)
        fp.write_text(final)
        FILE_READ_STATE[str(fp)] = fp.stat().st_mtime

        label = "replace_all" if replace_all else ("fuzzy match" if base != normalized else "exact")
        return f"Edited {path} ({label}, {count} replacement{'s' if count != 1 else ''})\n{diff_output}"
    except Exception as e:
        return f"Error: {e}"


def _seek_context(lines: list[str], context_lines: list[str], start_from: int = 0) -> int | None:
    """Find the position in lines where context_lines match, starting from start_from.
    Uses 4-level tolerance: exact -> Unicode-normalized -> trailing-whitespace-trimmed -> fully-trimmed.
    Returns the line index or None.
    """
    if not context_lines:
        return start_from

    def _match_line(file_line: str, ctx_line: str) -> bool:
        if file_line == ctx_line:
            return True
        if _normalize_unicode(file_line) == _normalize_unicode(ctx_line):
            return True
        if file_line.rstrip() == ctx_line.rstrip():
            return True
        if file_line.strip() == ctx_line.strip():
            return True
        return False

    for i in range(start_from, len(lines)):
        if _match_line(lines[i], context_lines[0]):
            if len(context_lines) == 1:
                return i
            all_match = True
            for j, ctx in enumerate(context_lines[1:], 1):
                if i + j >= len(lines) or not _match_line(lines[i + j], ctx):
                    all_match = False
                    break
            if all_match:
                return i
    return None


def _parse_patch(patch_text: str) -> list[dict]:
    """Parse a V4A-style patch into a list of hunks.
    Each hunk: {"context": [...], "changes": [("-", line), ("+", line), (" ", line), ...]}
    """
    hunks = []
    current_context = []
    current_changes = []

    for raw_line in patch_text.split("\n"):
        if raw_line.startswith("@@"):
            if current_changes:
                hunks.append({"context": current_context, "changes": current_changes})
                current_context = []
                current_changes = []
            ctx_text = raw_line[2:].strip() if len(raw_line) > 2 else ""
            if ctx_text:
                current_context.append(ctx_text)
        elif raw_line.startswith("-"):
            current_changes.append(("-", raw_line[1:]))
        elif raw_line.startswith("+"):
            current_changes.append(("+", raw_line[1:]))
        elif raw_line.startswith(" "):
            current_changes.append((" ", raw_line[1:]))

    if current_changes:
        hunks.append({"context": current_context, "changes": current_changes})

    return hunks


def run_apply_patch(path: str, patch: str) -> str:
    try:
        fp = safe_path(path)

        read_err = _check_read_state(fp)
        if read_err:
            return read_err

        raw_content = fp.read_text()
        bom, content = _strip_bom(raw_content)
        original_ending = _detect_line_ending(content)
        normalized = _normalize_to_lf(content)
        lines = normalized.split("\n")

        hunks = _parse_patch(patch)
        if not hunks:
            return "Error: No valid hunks found in patch. Use @@ for context and +/- for changes."

        cursor = 0

        for hi, hunk in enumerate(hunks):
            ctx = hunk["context"]
            changes = hunk["changes"]

            if ctx:
                pos = _seek_context(lines, ctx, cursor)
                if pos is None:
                    return (
                        f"Error: Could not locate context for hunk {hi + 1} in {path}. "
                        f"Context: {ctx!r}"
                    )
                cursor = pos + len(ctx)
            else:
                if hi == 0:
                    cursor = 0

            apply_at = cursor
            i = apply_at
            result_insert = []
            for op, text in changes:
                if op == "-":
                    if i >= len(lines):
                        return (
                            f"Error: Hunk {hi + 1} tries to delete beyond end of file. "
                            f"Expected: {text!r}"
                        )
                    file_line = lines[i]
                    if not (file_line == text or file_line.strip() == text.strip()
                            or _normalize_unicode(file_line) == _normalize_unicode(text)):
                        return (
                            f"Error: Hunk {hi + 1} delete mismatch at line {i + 1}. "
                            f"Expected: {text!r}, Found: {file_line!r}"
                        )
                    i += 1
                elif op == "+":
                    result_insert.append(text)
                elif op == " ":
                    if i >= len(lines):
                        return (
                            f"Error: Hunk {hi + 1} context line beyond end of file. "
                            f"Expected: {text!r}"
                        )
                    i += 1
                    result_insert.append(lines[i - 1])

            lines[apply_at:i] = result_insert
            cursor = apply_at + len(result_insert)

        new_content = "\n".join(lines)
        diff_output = _generate_diff(normalized, new_content)
        final = bom + _restore_line_endings(new_content, original_ending)
        fp.write_text(final)
        FILE_READ_STATE[str(fp)] = fp.stat().st_mtime

        return f"Patched {path} ({len(hunks)} hunk{'s' if len(hunks) != 1 else ''})\n{diff_output}"
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
            return f"No files matching '{pattern}' in {_display_path(base)} [workspace: {WORKSPACE_DIR}]"
        header = f"[searched in: {_display_path(base)}, workspace: {WORKSPACE_DIR}]"
        lines = [header] + [_display_path(m) for m in matches[:50]]
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
            if r.returncode > 1:
                err = r.stderr.strip() or "rg failed"
                return f"Error: {err}"
            out = r.stdout.strip()
            if not out:
                return f"No matches for '{pattern}'"
            lines = out.splitlines()[:max_results]
            return "\n".join(lines)
        except FileNotFoundError:
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
                            results.append(f"{_display_path(fp)}:{i}:{line.rstrip()}")
                            if len(results) >= max_results:
                                break
                except (UnicodeDecodeError, PermissionError):
                    continue
                if len(results) >= max_results:
                    break
            return "\n".join(results) if results else f"No matches for '{pattern}'"
    except Exception as e:
        return f"Error: {e}"


def run_background(command: str) -> str:
    return BG.run(command)


def check_background(task_id: str = None) -> str:
    return BG.check(task_id)


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw.get("command"), kw.get("restart", False)),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("offset"), kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(
        kw["path"], kw["old_text"], kw["new_text"], kw.get("replace_all", False)
    ),
    "apply_patch": lambda **kw: run_apply_patch(kw["path"], kw["patch"]),
    "glob": lambda **kw: run_glob(kw["pattern"], kw.get("path", ".")),
    "grep": lambda **kw: run_grep(kw["pattern"], kw.get("path", "."), kw.get("include"), kw.get("max_results", 50)),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
    "background_run": lambda **kw: run_background(kw["command"]),
    "check_background": lambda **kw: check_background(kw.get("task_id")),
}

if IMAGE_GENERATION_CONFIG is not None:
    TOOL_HANDLERS["generate_image"] = lambda **kw: run_generate_image(
        kw["prompt"],
        kw.get("negative_prompt"),
        kw.get("size"),
        kw.get("prompt_extend"),
        kw.get("watermark"),
        kw.get("output_dir"),
        kw.get("filename_prefix"),
    )

if IMAGE_EDIT_CONFIG is not None:
    TOOL_HANDLERS["edit_image"] = lambda **kw: run_edit_image(
        kw["image_paths"],
        kw["prompt"],
        kw.get("negative_prompt"),
        kw.get("size"),
        kw.get("n"),
        kw.get("prompt_extend"),
        kw.get("watermark"),
        kw.get("output_dir"),
        kw.get("filename_prefix"),
    )

if WEB_SEARCH_CONFIG is not None:
    TOOL_HANDLERS["web_search"] = lambda **kw: run_web_search(
        kw["query"],
        kw.get("max_results"),
        kw.get("language"),
        kw.get("categories"),
    )

BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in a persistent bash session rooted at workspace. State (cwd, env vars) persists across calls. Set restart=true to reset.",
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
        "description": "Read file contents with line numbers, or list directory entries. Relative paths are workspace-relative; use @agent/<path> to read from agent home.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path in workspace, absolute path, or explicit @workspace/<...> / @agent/<...>"},
                "offset": {"type": "integer", "description": "Start line number (1-indexed)"},
                "limit": {"type": "integer", "description": "Max number of lines to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file with the given content. Creates parent directories automatically. "
            "IMPORTANT: Both 'path' and 'content' parameters are REQUIRED and must be provided in a single valid JSON object. "
            "For large files, write the complete content in one call — do not split across multiple calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative by default, or explicit @workspace/<...> / @agent/<...>, or absolute path",
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write. Must be a valid JSON string (escape newlines as \\n, quotes as \\\")",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file (old_text->new_text). Relative paths are workspace-relative by default. old_text must be unique unless replace_all=true. You MUST read_file before editing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Text to find and replace (must be unique unless replace_all=true)"},
                "new_text": {"type": "string", "description": "Replacement text (must differ from old_text)"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "apply_patch",
        "description": (
            "Apply a patch to a file using @@ context lines for positioning and +/- for line changes. "
            "Efficient for large edits — only specify a few context lines to locate the change, not the entire old text. "
            "You MUST read_file before patching.\n"
            "Format:\n"
            "@@ context line to locate position\n"
            "-line to remove\n"
            "+line to add\n"
            " unchanged context line (space prefix)\n"
            "@@ next change location\n"
            "-old line\n"
            "+new line"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to patch (workspace-relative by default, supports @workspace/<...> and @agent/<...>)"},
                "patch": {"type": "string", "description": "Patch content with @@ context, -deletions, +additions"},
            },
            "required": ["path", "patch"],
        },
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern, sorted by modification time (newest first). Path defaults to workspace root.",
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
        "description": "Search file contents by regex pattern. Path defaults to workspace root. Uses ripgrep if available, else Python re fallback.",
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

BASE_TOOLS += _optional_generate_image_tools()
BASE_TOOLS += _optional_edit_image_tools()
BASE_TOOLS += _optional_web_search_tools()

CHILD_TOOLS = BASE_TOOLS
EXPLORE_TOOLS = [
    t for t in BASE_TOOLS if t["name"] not in ("write_file", "edit_file", "apply_patch", "generate_image", "edit_image")
]
PARENT_TOOLS = BASE_TOOLS + [
    {
        "name": "sub_agent",
        "description": "Spawn a subagent with fresh context. mode='explore' for read-only investigation, mode='execute' for tasks that modify files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string", "description": "Short label for logging"},
                "mode": {
                    "type": "string",
                    "enum": ["explore", "execute"],
                    "description": "explore=read-only, execute=read-write (default: execute)",
                },
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
    {
        "name": "background_run",
        "description": "Run a shell command in your private background worker.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "check_background",
        "description": "Check your private background tasks; omit task_id to list all.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
]

