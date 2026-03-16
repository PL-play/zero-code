import os
from pathlib import Path

from dotenv import load_dotenv
from llm_client.capabilities import capability_overrides_from_env
from llm_client.interface import OpenAICompatibleChatConfig
from llm_client.llm_factory import OpenAICompatibleChatLLMService
from llm_client.qwen_image import qwen_image_config_from_env, qwen_image_edit_config_from_env
from llm_client.web_search import web_search_config_from_env

AGENT_DIR = Path(__file__).resolve().parent.parent

# Always load .env from agent home, regardless of CWD.
_env_path = AGENT_DIR / ".env"
load_dotenv(_env_path, override=True)
WORKSPACE_ENV_KEYS = ("ZERO_CODE_WORKSPACE", "ZERO_CODE_WORKDIR", "VSCODE_WORKSPACE_FOLDER")


def _resolve_workspace_dir() -> Path:
    for key in WORKSPACE_ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return Path.cwd().resolve()


WORKSPACE_DIR = _resolve_workspace_dir()
# Backward-compatible alias used by older modules/tests.
WORKDIR = WORKSPACE_DIR
DEFAULT_SKILLS_DIR = AGENT_DIR / ".skills"

if WORKSPACE_DIR == AGENT_DIR:
    import sys
    print(
        f"⚠ WARNING: Workspace and agent home are the same directory: {WORKSPACE_DIR}\n"
        "  This means file operations will target the agent's own code.\n"
        "  Set ZERO_CODE_WORKSPACE to a different directory, or run from a project directory.",
        file=sys.stderr,
    )


def _resolve_skills_dir() -> Path:
    raw = (os.environ.get("ZERO_CODE_SKILLS_DIR") or "").strip()
    if not raw:
        return DEFAULT_SKILLS_DIR

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (AGENT_DIR / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if candidate.exists() and candidate.is_dir():
        return candidate
    return DEFAULT_SKILLS_DIR


SKILLS_DIR = _resolve_skills_dir()
AGENT_RW_ALLOWLIST = (
    AGENT_DIR / ".cache",
    AGENT_DIR / "logs",
    SKILLS_DIR,
)

MODEL = os.environ["OPENAI_COMPAT_MODEL"]

_cfg = OpenAICompatibleChatConfig(
    model=MODEL,
    base_url=os.environ["OPENAI_COMPAT_BASE_URL"],
    api_key=os.environ["OPENAI_COMPAT_API_KEY"],
    capability_overrides=capability_overrides_from_env(os.environ),
)
client = OpenAICompatibleChatLLMService(_cfg)
IMAGE_GENERATION_CONFIG = qwen_image_config_from_env(os.environ)
IMAGE_EDIT_CONFIG = qwen_image_edit_config_from_env(os.environ)
WEB_SEARCH_CONFIG = web_search_config_from_env(os.environ)


def _is_in_agent_rw_allowlist(path: Path) -> bool:
    resolved = path.resolve()
    for allowed_root in AGENT_RW_ALLOWLIST:
        try:
            if resolved.is_relative_to(allowed_root.resolve()):
                return True
        except Exception:
            continue
    return False


def safe_path(p: str, purpose: str = "rw") -> Path:
    raw_input = (p or "").strip()
    if not raw_input:
        raise ValueError("Path is required")

    # Optional explicit root selectors to disambiguate workspace vs agent home.
    if raw_input.startswith("@workspace/"):
        candidate = (WORKSPACE_DIR / raw_input[len("@workspace/") :]).resolve()
    elif raw_input.startswith("@agent/"):
        candidate = (AGENT_DIR / raw_input[len("@agent/") :]).resolve()
    else:
        raw = Path(raw_input).expanduser()
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (WORKSPACE_DIR / raw).resolve()

    if candidate.is_relative_to(WORKSPACE_DIR):
        return candidate

    if candidate.is_relative_to(AGENT_DIR):
        if _is_in_agent_rw_allowlist(candidate):
            return candidate
        # Try to suggest the workspace-relative equivalent
        try:
            rel_to_agent = candidate.relative_to(AGENT_DIR)
            workspace_alt = WORKSPACE_DIR / rel_to_agent
            hint = (
                f" Did you mean the workspace file instead? "
                f"Try: {rel_to_agent} (resolves to {workspace_alt})"
            )
        except ValueError:
            hint = ""
        raise ValueError(
            f"Access to agent home is restricted.{hint} "
            f"Workspace is at {WORKSPACE_DIR}, not {AGENT_DIR}. "
            "Use relative paths (they default to workspace) or @workspace/<path>."
        )

    raise ValueError(
        f"Path escapes allowed directories: {p}. "
        f"Workspace: {WORKSPACE_DIR}. Use relative paths or @workspace/<path>."
    )

