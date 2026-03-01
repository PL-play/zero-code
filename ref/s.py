#!/usr/bin/env python3
"""
s.py - Skills + Subagent + Task System + Context Compact

Full-featured coding agent with:
- Skills loading (layered injection)
- Multi-workspace safe path support
- Parent/child agent with subagent delegation
- Persistent task system (JSON files in .tasks/, survives context compression)
  with dependency graph (blockedBy/blocks)
- Three-layer context compaction:
  1) micro_compact (every turn)
  2) auto_compact when context too large
  3) manual compact tool
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# Initialize workspaces: default current dir + manual configuration
_default_workdir = Path.cwd()
_manual_workdirs = os.getenv("WORKDIRS", "").split(";") if os.getenv("WORKDIRS") else []
WORKDIRS = [_default_workdir] + [Path(d).resolve() for d in _manual_workdirs if d.strip()]
WORKDIR = WORKDIRS[0]  # Backward compatibility

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
SKILLS_DIR = WORKDIR / "skills"

# Context compact config
THRESHOLD = int(os.getenv("CONTEXT_COMPACT_THRESHOLD", "50000"))
KEEP_RECENT = int(os.getenv("CONTEXT_COMPACT_KEEP_RECENT", "10"))
TRANSCRIPT_DIR = WORKDIR / "transcripts"
LOGS_DIR = WORKDIR / "logs"
TASKS_DIR = WORKDIR / ".tasks"
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
MICRO_COMPACT_CURSOR = 0
MESSAGE_LOG_PATH = LOGS_DIR / f"messages_{int(time.time())}.jsonl"

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


def parse_tool_log_level(value: str) -> int:
    v = (value or "1").strip().lower()
    if v in {"0", "false", "off", "no", "quiet"}:
        return 0
    if v in {"2", "mid", "medium", "input", "input-only", "params"}:
        return 2
    return 1


TOOL_LOG_LEVEL = parse_tool_log_level(os.getenv("TOOL_LOG_VERBOSE", "1"))


def append_messages_jsonl(entries: list) -> None:
    if not entries:
        return
    LOGS_DIR.mkdir(exist_ok=True)
    with open(MESSAGE_LOG_PATH, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")


class BackgroundManager:
    """Background command runner with notification queue."""

    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"

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
                cwd=WORKDIR,
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

        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        with self._lock:
            self._notification_queue.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "command": command[:80],
                    "result": (output or "(no output)")[:500],
                }
            )

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

    def drain_notifications(self) -> list:
        with self._lock:
            notifications = list(self._notification_queue)
            self._notification_queue.clear()
        return notifications


BG = BackgroundManager()


class TaskManager:
    """Persistent task system with dependency graph.
    Tasks are stored as JSON files in .tasks/ so they survive context compression.
    """

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "blocks": [],
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        done = sum(1 for t in tasks if t["status"] == "completed")
        lines.append(f"\n({done}/{len(tasks)} completed)")
        return "\n".join(lines)


TASKS = TaskManager(TASKS_DIR)


@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class MessageBus:
    """File-based JSONL inbox bus with lock-protected send/drain."""

    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _inbox_path(self, name: str) -> Path:
        return self.dir / f"{name}.jsonl"

    def _lock_path(self, inbox_path: Path) -> Path:
        return inbox_path.with_suffix(inbox_path.suffix + ".lock")

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"

        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)

        inbox_path = self._inbox_path(to)
        with file_lock(self._lock_path(inbox_path)):
            with open(inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self._inbox_path(name)
        if not inbox_path.exists():
            return []

        messages = []
        with file_lock(self._lock_path(inbox_path)):
            if not inbox_path.exists():
                return []
            content = inbox_path.read_text(encoding="utf-8").strip()
            if content:
                for line in content.splitlines():
                    if line:
                        messages.append(json.loads(line))
            inbox_path.write_text("", encoding="utf-8")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


class TeammateManager:
    """Persistent named teammates with lock-safe config updates."""

    def __init__(self, team_dir: Path, bus: MessageBus):
        self.dir = team_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config_lock_path = self.dir / "config.lock"
        self.bus = bus
        self.threads = {}
        self._mem_lock = threading.Lock()
        if not self.config_path.exists():
            self._save_config_locked({"team_name": "default", "members": []})

    def _load_config_locked(self) -> dict:
        with file_lock(self.config_lock_path):
            if self.config_path.exists():
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            return {"team_name": "default", "members": []}

    def _save_config_locked(self, config: dict):
        with file_lock(self.config_lock_path):
            self.config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _find_member(self, config: dict, name: str) -> dict:
        for member in config["members"]:
            if member["name"] == name:
                return member
        return None

    def _contacts_data(self, requester: str, config: dict = None) -> dict:
        if config is None:
            config = self._load_config_locked()
        contacts = [{"name": "lead", "role": "lead", "status": "available"}]
        for member in config["members"]:
            if member["name"] != requester:
                contacts.append(
                    {
                        "name": member["name"],
                        "role": member["role"],
                        "status": member["status"],
                    }
                )
        return {
            "team_name": config.get("team_name", "default"),
            "self": requester,
            "contacts": contacts,
        }

    def list_contacts(self, requester: str) -> str:
        return json.dumps(self._contacts_data(requester), indent=2, ensure_ascii=False)

    def spawn(self, name: str, role: str, prompt: str) -> str:
        with self._mem_lock:
            config = self._load_config_locked()
            member = self._find_member(config, name)
            if member:
                if member["status"] not in ("idle", "shutdown"):
                    return f"Error: '{name}' is currently {member['status']}"
                member["status"] = "working"
                member["role"] = role
            else:
                member = {"name": name, "role": role, "status": "working"}
                config["members"].append(member)
            self._save_config_locked(config)

            contacts = self._contacts_data(name, config)
            contact_names = ", ".join(c["name"] for c in contacts["contacts"])
            welcome = (
                f"Welcome, {name} ({role}). You are now in team '{contacts['team_name']}'.\n"
                "Communication rules:\n"
                "- You can always send updates to 'lead'.\n"
                "- Use list_contacts to refresh available recipients.\n"
                f"Current contacts: {contact_names}"
            )
            self.bus.send("lead", name, welcome, "message", extra={"kind": "welcome"})

            thread = threading.Thread(
                target=self._teammate_loop,
                args=(name, role, prompt),
                daemon=True,
            )
            self.threads[name] = thread
            thread.start()

        return f"Spawned '{name}' (role: {role})"

    def _set_member_status(self, name: str, status: str):
        with self._mem_lock:
            config = self._load_config_locked()
            member = self._find_member(config, name)
            if not member:
                return
            member["status"] = status
            self._save_config_locked(config)

    def _teammate_loop(self, name: str, role: str, prompt: str):
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            "Use send_message to communicate. Use list_contacts to discover valid recipients (including lead). Complete your task."
        )
        messages = [{"role": "user", "content": prompt}]
        append_messages_jsonl([
            {
                "role": "user",
                "content": f"[teammate:{name}] {prompt}",
            }
        ])
        teammate_bg = BackgroundManager()
        teammate_tool_handlers = {
            "background_run": lambda **kw: teammate_bg.run(kw["command"]),
            "check_background": lambda **kw: teammate_bg.check(kw.get("task_id")),
        }
        tools = self._teammate_tools()
        response = None
        hit_limit = False

        for _ in range(50):
            notifications = teammate_bg.drain_notifications()
            if notifications and messages:
                notification_text = "\n".join(
                    f"[team-bg:{item['task_id']}] {item['status']}: {item['result']}"
                    for item in notifications
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"<background-results>\n{notification_text}\n</background-results>",
                    }
                )
                messages.append({"role": "assistant", "content": "Noted background results."})
                append_messages_jsonl([messages[-2], messages[-1]])

            inbox = self.bus.read_inbox(name)
            for msg in inbox:
                messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                append_messages_jsonl([messages[-1]])

            try:
                response = client.messages.create(
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:
                break

            assistant_msg = {"role": "assistant", "content": [block.model_dump() for block in response.content]}
            messages.append(assistant_msg)
            append_messages_jsonl([messages[-1]])

            has_tool_use = any(getattr(block, "type", None) == "tool_use" for block in response.content)
            if not has_tool_use:
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name in teammate_tool_handlers:
                    handler = teammate_tool_handlers[block.name]
                    output = handler(**block.input)
                else:
                    output = self._exec(name, block.name, block.input)
                _log_tool_call(block.name, block.id, block.input, output, prefix=f"  [team:{name}] ")
                print(f"  [{name}] {block.name}: {str(output)[:120]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000],
                })
            messages.append({"role": "user", "content": results})
            append_messages_jsonl([messages[-1]])
        else:
            hit_limit = True

        if response is not None and hit_limit:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have reached your maximum teammate loop rounds and must stop now.\n"
                        "Please summarize:\n"
                        "1) What you accomplished\n"
                        "2) Key findings from tools\n"
                        "3) What remains unfinished/uncertain\n"
                        "Clearly note this may be incomplete."
                    ),
                }
            )
            append_messages_jsonl([messages[-1]])
            summary_response = client.messages.create(
                model=MODEL,
                system=sys_prompt,
                messages=messages,
                tools=[],
                max_tokens=8000,
            )
            summary_msg = {
                "role": "assistant",
                "content": [block.model_dump() for block in summary_response.content],
            }
            append_messages_jsonl([summary_msg])
            summary_text = "".join(
                b.text for b in summary_response.content if hasattr(b, "text")
            )
            if summary_text:
                print(
                    f"  [{name}] [INCOMPLETE - hit teammate round limit] "
                    f"{summary_text[:300]}{'...' if len(summary_text) > 300 else ''}"
                )

        config = self._load_config_locked()
        member = self._find_member(config, name)
        if member and member.get("status") != "shutdown":
            self._set_member_status(name, "idle")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "read_file":
            return run_read(args["path"], args.get("limit"))
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "load_skill":
            return SKILL_LOADER.get_content(args["name"])
        if tool_name == "send_message":
            return self.bus.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(self.bus.read_inbox(sender), indent=2, ensure_ascii=False)
        if tool_name == "list_contacts":
            return self.list_contacts(sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        return [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read file contents.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write content to file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
            {
                "name": "edit_file",
                "description": "Replace exact text in file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
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
            {
                "name": "send_message",
                "description": "Send message to a teammate inbox.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                        "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
                    },
                    "required": ["to", "content"],
                },
            },
            {
                "name": "read_inbox",
                "description": "Read and drain your inbox.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_contacts",
                "description": "List allowed recipients for messaging (includes lead).",
                "input_schema": {"type": "object", "properties": {}},
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

    def list_all(self) -> str:
        config = self._load_config_locked()
        if not config["members"]:
            return "No teammates."
        lines = [f"Team: {config['team_name']}"]
        for member in config["members"]:
            lines.append(f"  {member['name']} ({member['role']}): {member['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        config = self._load_config_locked()
        return [member["name"] for member in config["members"]]


BUS = MessageBus(INBOX_DIR)
TEAM = TeammateManager(TEAM_DIR, BUS)


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
            rel_path = Path(skill["path"]).relative_to(WORKDIR)
            line = f"  - {name}: {desc} (path: {rel_path})"
            if tags:
                line += f" [{tags}]"
            lines.append(line)

        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"

        rel_path = Path(skill["path"]).relative_to(WORKDIR)
        return (
            f"<skill name=\"{name}\" path=\"{rel_path}\">\n"
            f"Source: {rel_path}\n\n"
            f"{skill['body']}\n"
            f"</skill>"
        )


SKILL_LOADER = SkillLoader(SKILLS_DIR)

SYSTEM = f"""You are a coding agent at {WORKDIR}. Use task tools to plan and track work.

Core operating rule:
- You are a team lead, not a solo implementer. For non-trivial requests, default to planning + delegation first.

Mandatory workflow for non-trivial requests:
1) Use task_create to split work into concrete subtasks before major execution.
2) Use task_update to keep exactly one active lead task in_progress at a time.
3) Delegate independent/deep tasks via spawn_teammate instead of doing all work yourself.
4) Assign each teammate a clear scope, deliverable, and expected output format.
5) Use send_message to coordinate and unblock teammates; use read_inbox every turn to collect results.
6) Merge teammate outputs, then mark related tasks completed via task_update.
7) Use task_list repeatedly to decide the next best action.

Delegation triggers (must delegate when any is true):
- There are 2+ independent subtasks.
- Work involves cross-file investigation, comparison, or long-running checks.
- You are blocked waiting on background or external tool results.

Lead execution limits:
- Do NOT execute all subtasks yourself when delegation is possible.
- Keep lead focused on orchestration, integration, and final quality checks.
- Prefer parallel teammate execution over sequential solo execution.

Task system:
- Tasks persist as JSON files in .tasks/ — they survive context compression.
- Each task can have blockedBy (dependencies) and blocks (dependents).
- Completing a task automatically unblocks its dependents.
- Always check task_list before starting new work to avoid duplicates.

Background tasks:
- background_run starts a command asynchronously and returns immediately.
- check_background checks one task or lists all tasks.
- Completed background results are automatically injected before each model call.

Team tools:
- spawn_teammate creates persistent workers in separate threads.
- send_message/read_inbox/broadcast are file-backed and lock-safe for concurrent access.
- Use list_teammates to monitor teammate status and reassign work.

Per-turn checklist:
- Have I updated task state?
- Is there work I should delegate right now?
- Have I checked inbox and integrated teammate outputs?
- Am I acting as lead (orchestrating) rather than doing all implementation alone?

Skills available:
{SKILL_LOADER.get_descriptions()}"""

SUBAGENT_SYSTEM = f"""You are a coding subagent at {WORKDIR}.
Use load_skill when needed. Complete the given task, then summarize findings.
Return concise, evidence-based results with concrete file paths/symbols when applicable."""


def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4


def auto_compact(messages: list) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

    print(f"[transcript saved: {transcript_path}]")

    conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    response = client.messages.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": (
                "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made, 4) Open tasks. "
                "Be concise but preserve critical details.\n\n" + conversation_text
            ),
        }],
        max_tokens=8000,
    )

    summary = response.content[0].text

    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]


def safe_path(p: str) -> Path:
    for workdir in WORKDIRS:
        path = (workdir / p).resolve()
        try:
            if path.is_relative_to(workdir):
                return path
        except ValueError:
            continue
    raise ValueError(f"Path '{p}' escapes all workspaces: {WORKDIRS}")


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("addBlocks")),
    "task_list": lambda **kw: TASKS.list_all(),
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
    "background_run": lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "spawn_teammate": lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates": lambda **kw: TEAM.list_all(),
    "send_message": lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox": lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False),
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "compact": lambda **kw: "Manual compression requested.",
}

BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
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
    {
        "name": "background_run",
        "description": "Run a shell command in background thread and return task id immediately.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "check_background",
        "description": "Check background task status. Omit task_id to list all.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
]

CHILD_TOOLS = BASE_TOOLS

PARENT_TOOLS = BASE_TOOLS + [
    {
        "name": "spawn_teammate",
        "description": "Spawn a persistent teammate that runs in its own thread.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    {
        "name": "list_teammates",
        "description": "List all teammates with name, role and status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Send a message to a teammate inbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
                "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "read_inbox",
        "description": "Read and drain lead inbox.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "broadcast",
        "description": "Broadcast a message to all teammates.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    # {
    #     "name": "subagent",
    #     "description": "Deprecated: teammate workflow replaces subagent.",
    #     "input_schema": {
    #         "type": "object",
    #         "properties": {
    #             "prompt": {"type": "string", "description": "Detailed instruction for the subagent"},
    #             "description": {"type": "string", "description": "Short label for logging"},
    #         },
    #         "required": ["prompt"],
    #     },
    # },
    {
        "name": "task_create",
        "description": "Create a new task. Tasks persist as JSON files and survive context compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "Detailed description"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a task's status or dependencies. Completing a task auto-unblocks dependents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "addBlockedBy": {"type": "array", "items": {"type": "integer"}, "description": "Task IDs this task depends on"},
                "addBlocks": {"type": "array", "items": {"type": "integer"}, "description": "Task IDs that depend on this task"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List all tasks with status and dependency info.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_get",
        "description": "Get full details of a task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "compact",
        "description": "Trigger manual conversation compression. Use when you want to proactively reduce context size, especially after long tool results or finish a task. The agent will summarize the conversation so far and continue with a fresh context.",
        "input_schema": {
            "type": "object",
            "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}},
        },
    },
]


def _log_tool_call(tool_name: str, tool_id: str, tool_input: dict, output: str, prefix: str = ""):
    """Shared tool-call logging used by both agent_loop and run_subagent."""
    output_text = str(output)
    tag = f"{prefix}[{tool_name}]" if prefix else f"[{tool_name}]"

    if TOOL_LOG_LEVEL in {1, 2}:
        input_payload = json.dumps(tool_input, ensure_ascii=False, indent=2)
        print(f"\n===== TOOL CALL START {tag} id={tool_id} =====")
        print("INPUT:")
        print(input_payload)

    if TOOL_LOG_LEVEL == 1:
        print("OUTPUT:")
        if len(output_text) > 500:
            print(output_text[:500])
            print(f"... (truncated {len(output_text) - 500} chars)")
        else:
            print(output_text)
        print(f"===== TOOL CALL END   {tag} id={tool_id} =====")
    elif TOOL_LOG_LEVEL == 2:
        print(f"OUTPUT: ({len(output_text)} chars)")
        preview = output_text.replace("\n", " ")[:160]
        print(f"PREVIEW: {preview}")
        print(f"===== TOOL CALL END   {tag} id={tool_id} =====")
    elif TOOL_LOG_LEVEL == 0:
        print(f"{prefix}> {tool_name}: {output_text[:200]}")


def run_subagent(prompt: str, max_rounds: int = 50) -> str:
    sub_bg = BackgroundManager()

    sub_tool_handlers = {
        "background_run": lambda **kw: sub_bg.run(kw["command"]),
        "check_background": lambda **kw: sub_bg.check(kw.get("task_id")),
    }

    sub_messages = [{"role": "user", "content": prompt}]
    append_messages_jsonl([sub_messages[-1]])
    response = None
    hit_limit = False

    for round_idx in range(max_rounds):
        sub_notifications = sub_bg.drain_notifications()
        if sub_notifications and sub_messages:
            sub_notification_text = "\n".join(
                f"[sub-bg:{item['task_id']}] {item['status']}: {item['result']}"
                for item in sub_notifications
            )
            sub_messages.append(
                {
                    "role": "user",
                    "content": f"<background-results>\n{sub_notification_text}\n</background-results>",
                }
            )
            append_messages_jsonl([sub_messages[-1]])
            sub_messages.append({"role": "assistant", "content": "Noted background results."})
            append_messages_jsonl([sub_messages[-1]])

        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        
        # Convert Message object to dict before appending
        assistant_msg = {"role": "assistant", "content": [block.model_dump() for block in response.content]}
        sub_messages.append(assistant_msg)
        append_messages_jsonl([sub_messages[-1]])

        # Print assistant text output if any
        for block in response.content:
            if hasattr(block, "text") and block.text:
                print(f"  [subagent] {block.text[:300]}{'...' if len(block.text) > 300 else ''}")

        has_tool_use = any(getattr(block, "type", None) == "tool_use" for block in response.content)
        if not has_tool_use:
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name in sub_tool_handlers:
                    handler = sub_tool_handlers[block.name]
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                _log_tool_call(block.name, block.id, block.input, output, prefix="  [sub] ")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
        append_messages_jsonl([sub_messages[-1]])
    else:
        # Loop exhausted without natural stop — force a summary
        hit_limit = True

    if response is None:
        return "(no summary)"

    if hit_limit:
        print(f"[subagent] hit {max_rounds}-round limit, forcing summary")
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
        append_messages_jsonl([sub_messages[-1]])
        summary_response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=[],  # No tools — force text-only response
            max_tokens=8000,
        )
        summary_msg = {"role": "assistant", "content": [block.model_dump() for block in summary_response.content]}
        append_messages_jsonl([summary_msg])
        text = "".join(b.text for b in summary_response.content if hasattr(b, "text"))
        return f"[INCOMPLETE - hit {max_rounds}-round limit]\n{text}" if text else "(forced stop, no summary)"

    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


def agent_loop(messages: list) -> str:

    while True:
        # micro_compact(messages)

        # if estimate_tokens(messages) > THRESHOLD:
        #     print("[auto_compact triggered]")
        #     messages[:] = auto_compact(messages)

        notifications = BG.drain_notifications()
        if notifications and messages:
            notification_text = "\n".join(
                f"[bg:{item['task_id']}] {item['status']}: {item['result']}"
                for item in notifications
            )
            messages.append({
                "role": "user",
                "content": f"<background-results>\n{notification_text}\n</background-results>",
            })
            append_messages_jsonl([messages[-1]])
            messages.append({"role": "assistant", "content": "Noted background results."})
            append_messages_jsonl([messages[-1]])

        inbox = BUS.read_inbox("lead")
        if inbox and messages:
            messages.append({
                "role": "user",
                "content": f"<inbox>\n{json.dumps(inbox, indent=2, ensure_ascii=False)}\n</inbox>",
            })
            append_messages_jsonl([messages[-1]])
            messages.append({"role": "assistant", "content": "Noted inbox messages."})
            append_messages_jsonl([messages[-1]])

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )
        
        # Convert Message object to dict before appending
        assistant_msg = {"role": "assistant", "content": [block.model_dump() for block in response.content]}
        messages.append(assistant_msg)
        append_messages_jsonl([messages[-1]])

        has_tool_use = any(getattr(block, "type", None) == "tool_use" for block in response.content)
        if not has_tool_use:
            return "\n".join(block.text for block in response.content if hasattr(block, "text"))

        results = []
        manual_compact = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "compact":
                manual_compact = True
                output = "Compressing..."
            else:
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"

            _log_tool_call(block.name, block.id, block.input, output)

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})


        messages.append({"role": "user", "content": results})
        append_messages_jsonl([messages[-1]])

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)
            append_messages_jsonl(messages)


if __name__ == "__main__":
    history = []
    print("# demo: 分析项目：/Users/zhangran/work/tsinghua/MEDLLM/django-backend-template 给出核心的时序图和UML图写入markdown文件")
    while True:
        try:
            query = input("\033[36ms >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        append_messages_jsonl([history[-1]])
        reply = agent_loop(history)
        print(reply)
        print()
