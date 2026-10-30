"""Settings for the improvement loop. Values come from environment variables or .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-5.5")
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "medium")      # reasoning effort for the agents
APP_PROJECT = os.getenv("PHOENIX_PROJECT", "support-bot")  # where the bot's traces live
LOOP_PROJECT = os.getenv("LOOP_PROJECT", "improvement-loop")  # where the agents' own traces go

DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "issues.db"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
REPORTS_DIR = DATA_DIR / "reports"
WORKTREES_DIR = REPO_ROOT / ".worktrees"

BASE_BRANCH = os.getenv("BASE_BRANCH", "main")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")        # "owner/name"; empty means local branches only
GITHUB_ACCOUNT = os.getenv("GITHUB_ACCOUNT", "")  # which logged-in gh account opens the PRs

# The fix agent may only edit these paths. It can never touch the health contract, the
# evaluator, the loop itself, or the help-center articles (product facts are owned by people).
EDITABLE_PATHS = ("support_bot/", "tests/")
PROTECTED_PATHS = ("support_bot/kb/",)
# Files no agent may read, even though they sit inside the repo.
SECRET_FILES = (".env",)
SKIPPED_DIRS = {".git", ".venv", ".worktrees", ".phoenix", "data", "__pycache__", ".pytest_cache",
                ".index", ".investigation"}

# Estimated USD per 1M tokens: (input, cached input, output). Third-party list prices,
# used only to print a cost estimate after each agent run.
PRICES = {
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
}
