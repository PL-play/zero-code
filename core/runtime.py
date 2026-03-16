import os
from pathlib import Path

from dotenv import load_dotenv
from llm_client.capabilities import capability_overrides_from_env
from llm_client.interface import OpenAICompatibleChatConfig
from llm_client.llm_factory import OpenAICompatibleChatLLMService
from llm_client.qwen_image import qwen_image_config_from_env, qwen_image_edit_config_from_env
from llm_client.web_search import web_search_config_from_env

load_dotenv(override=True)

AGENT_DIR = Path(__file__).resolve().parent.parent
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
        raise ValueError(
            "Access to agent home is restricted. Only allowlisted paths are permitted: "
            + ", ".join(str(p) for p in AGENT_RW_ALLOWLIST)
        )

    raise ValueError(f"Path escapes allowed directories: {p}")

