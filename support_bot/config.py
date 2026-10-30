"""Settings for the support bot. Values come from environment variables or the .env file."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
load_dotenv(REPO_ROOT / ".env")

BOT_MODEL = os.getenv("BOT_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
PHOENIX_PROJECT = os.getenv("PHOENIX_PROJECT", "support-bot")

KB_DIR = PACKAGE_DIR / "kb"
INDEX_DIR = PACKAGE_DIR / ".index"


def _git_version() -> str:
    """Short commit hash of the running code, so every trace says which version produced it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "dev"
    except (OSError, subprocess.SubprocessError):
        return "dev"


APP_VERSION = os.getenv("APP_VERSION") or _git_version()
