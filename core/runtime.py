import os
from pathlib import Path

from dotenv import load_dotenv
from llm_client.capabilities import capability_overrides_from_env
from llm_client.interface import OpenAICompatibleChatConfig
from llm_client.llm_factory import OpenAICompatibleChatLLMService
from llm_client.qwen_image import qwen_image_config_from_env, qwen_image_edit_config_from_env

load_dotenv(override=True)

WORKDIR = Path.cwd().resolve()
AGENT_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = AGENT_DIR / ".skills"

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


def safe_path(p: str) -> Path:
    raw = Path(p)
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (WORKDIR / p).resolve()
    if path.is_relative_to(WORKDIR) or path.is_relative_to(AGENT_DIR):
        return path
    raise ValueError(f"Path escapes allowed directories: {p}")

