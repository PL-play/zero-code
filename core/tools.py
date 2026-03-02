import queue
import re
import subprocess
import threading
from pathlib import Path

from core.runtime import WORKDIR, safe_path
from core.state import SKILL_LOADER, TODO

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
    idx = content.find(old_text)
    if idx != -1:
        return idx, idx + len(old_text)

    stripped = old_text.strip()
    for _, line in enumerate(content.splitlines(keepends=True)):
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


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw.get("command"), kw.get("restart", False)),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("offset"), kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(
        kw["path"], kw.get("old_text"), kw.get("new_text"), kw.get("insert_line"), kw.get("insert_text")
    ),
    "glob": lambda **kw: run_glob(kw["pattern"], kw.get("path", ".")),
    "grep": lambda **kw: run_grep(kw["pattern"], kw.get("path", "."), kw.get("include"), kw.get("max_results", 50)),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}

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
]

