import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd().resolve()
AGENT_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = AGENT_DIR / ".skills"

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


def safe_path(p: str) -> Path:
    raw = Path(p)
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (WORKDIR / p).resolve()
    if path.is_relative_to(WORKDIR) or path.is_relative_to(AGENT_DIR):
        return path
    raise ValueError(f"Path escapes allowed directories: {p}")

