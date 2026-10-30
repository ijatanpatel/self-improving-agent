"""The fix agent: given one issue, change the code inside an isolated git worktree.

It can read the repo and edit files only under an allowlist (support_bot/ and tests/,
never the help-center articles, the health contract or the evaluators). It has no
shell and no git: the loop runs the tests itself and does every git/GitHub step.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from loop import settings
from loop.agent import AgentResult, Tool, params, run_agent
from loop.workspace import Workspace

INSTRUCTIONS = """You are a careful software engineer fixing one production issue in Orbi, a \
RAG customer-support bot written in Python. You work in a separate copy of the repository \
(a git worktree). A human will review your change as a pull request before it is merged.

How to work:
1. Read the issue, then read the code it points to (read_file, search_code) before editing.
2. Make the smallest change that fixes the root cause. Match the surrounding style and \
comments. No unrelated refactors.
3. Add or update a unit test in tests/ that would have caught this problem. Tests must run \
offline: no network and no OpenAI calls (use pytest's monkeypatch to stub them).
4. Call run_tests and make sure every test passes.
5. Call finish with a short summary for the pull request description.

Rules:
- You may only edit files under support_bot/ and tests/, and never support_bot/kb/. The \
health contract, the evaluators and the loop are off-limits: fix the app, not the measurement.
- Never invent product facts (prices, limits, features) that are not in support_bot/kb/."""

FINISH = Tool(
    "finish", "Call when the fix is complete and run_tests passes.",
    params(summary={"type": "string", "description": "2-4 sentences for the PR description"},
           changes={"type": "array", "items": {"type": "string"},
                    "description": "one line per change, e.g. 'support_bot/x.py: ...'"},
           risks={"type": "string", "description": "anything the reviewer should double-check"}),
    run=lambda **_: "",
)


def run_tests(root: Path) -> tuple[bool, str]:
    """Run the test suite in the worktree. Tests get no API key, so they must stay offline."""
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root, env=env, capture_output=True, text=True, timeout=600,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-3000:]


def format_issue(issue: dict) -> str:
    evidence = "\n".join(f"- trace {e['trace_id']}: {e['observation']}" for e in issue["evidence"])
    code = "\n".join(f"- {c['path']}:{c['line']}: {c['why']}" for c in issue["code_locations"])
    fix = "\n".join(f"{n}. {step}" for n, step in enumerate(issue["proposed_fix"], 1))
    return (f"Issue #{issue['id']}: {issue['title']} [{issue['severity']}]\n"
            f"Broken rule: {issue['rule_id']} (related: {', '.join(issue['related_rules']) or 'none'}); "
            f"{issue['affected_requests']} of {issue['checked_requests']} requests affected.\n\n"
            f"Summary: {issue['summary']}\n\nRoot cause: {issue['root_cause']}\n\n"
            f"Evidence from production traces:\n{evidence}\n\nCode locations:\n{code}\n\n"
            f"Proposed fix (from the investigation; use your judgment):\n{fix}")


def run_fix(issue: dict, worktree: Path, transcript: Path) -> tuple[AgentResult, Workspace, str]:
    ws = Workspace(worktree, editable=settings.EDITABLE_PATHS, protected=settings.PROTECTED_PATHS)
    last_test = {"passed": False, "output": ""}

    def run_tests_tool() -> str:
        passed, output = run_tests(worktree)
        last_test.update(passed=passed, output=output)
        return ("PASSED\n" if passed else "FAILED\n") + output

    def validate(args: dict) -> str | None:
        if not ws.changed:
            return "You have not changed any files yet."
        passed, output = run_tests(worktree)  # never trust a claim that tests pass
        last_test.update(passed=passed, output=output)
        return None if passed else f"The test suite fails:\n{output}"

    tools = [
        Tool("read_file", "Read a file with line numbers (max 250 lines per call).",
             params(path={"type": "string"}, start_line={"type": ["integer", "null"]},
                    end_line={"type": ["integer", "null"]}), ws.read_file),
        Tool("search_code", "Regex search over repository files; returns path:line: text.",
             params(pattern={"type": "string"}, glob={"type": ["string", "null"]}), ws.search_code),
        Tool("list_files", "List repository files under a directory.",
             params(directory={"type": "string"}), ws.list_files),
        Tool("replace_in_file",
             "Replace one exact, unique snippet of a file. Copy old_text exactly, including "
             "indentation, with enough lines around it to be unique.",
             params(path={"type": "string"}, old_text={"type": "string"}, new_text={"type": "string"}),
             ws.replace_in_file),
        Tool("create_file", "Create a new file (for example a new test file).",
             params(path={"type": "string"}, content={"type": "string"}), ws.create_file),
        Tool("run_tests", "Run the whole pytest suite offline and show the result.", params(),
             run_tests_tool),
    ]
    result = run_agent(
        name="fix", instructions=INSTRUCTIONS, task=format_issue(issue) + "\n\nFix this issue.",
        tools=tools, finish=FINISH, validate=validate, transcript=transcript, max_turns=40,
    )
    return result, ws, last_test["output"]


if __name__ == "__main__":  # handy for debugging: python -m loop.fixer <worktree>
    print(json.dumps(run_tests(Path(sys.argv[1]))[0]))
