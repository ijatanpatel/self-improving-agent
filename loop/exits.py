"""Exits: what the loop does with an issue. Each exit runs at most once per issue.

- dataset:   the violating requests become a Phoenix dataset, a regression set to replay
- evaluator: a draft LLM-judge evaluator (YAML) that a human can switch on
- pr:        the fix agent edits a git worktree; the loop runs the tests, commits, pushes
             and opens a pull request for a human to review. Nothing is ever merged here.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from loop import settings
from loop.contract import check_contract, load_contract
from loop.fixer import run_fix, run_tests
from loop.store import IssueStore
from loop.telemetry import client, fetch_requests

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def git(*args: str, cwd: Path = settings.REPO_ROOT, env: dict | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


# ---- exit 1: regression dataset ------------------------------------------------------
def add_to_dataset(store: IssueStore, issue: dict, hours: float = 24) -> str:
    """Save the requests that broke this issue's rules as a Phoenix dataset."""
    if existing := store.get_exit(issue["id"], "dataset"):
        return existing["target"]
    contract = load_contract()
    requests = fetch_requests(settings.APP_PROJECT, hours)
    results = {r.rule_id: r for r in check_contract(contract, requests)}
    rules = [issue["rule_id"], *issue["related_rules"]]
    broken = {tid for rule in rules if rule in results for tid in results[rule].violations}

    examples, seen = [], set()
    for r in requests:
        channel = r.root.attributes.get("app.channel", "web")
        if r.trace_id in broken and (r.question, channel) not in seen:
            seen.add((r.question, channel))
            examples.append({
                "input": {"question": r.question, "channel": channel},
                "output": {"answer_before_fix": r.answer},
                "metadata": {"trace_id": r.trace_id, "span_id": r.root.span_id,
                             "app_version": r.root.attributes.get("app.version"),
                             "broken_rules": [x for x in rules if r.trace_id in results[x].violations]},
            })
    if not examples:
        raise RuntimeError("No violating requests found in the window; nothing to save.")
    name = f"issue-{issue['id']}-{slug(issue['rule_id'].split(':')[-1], 30)}"
    client().datasets.create_dataset(
        name=name, examples=examples[:25],
        dataset_description=f"Requests that broke {', '.join(rules)} (issue #{issue['id']}).",
    )
    store.record_exit(issue["id"], "dataset", name, {"examples": len(examples[:25])})
    return name


# ---- exit 2: draft evaluator ---------------------------------------------------------
def draft_evaluator(store: IssueStore, issue: dict, root: Path = settings.REPO_ROOT) -> str:
    """Write the agent's evaluator idea as a reviewable YAML draft (status: draft)."""
    idea = issue["evaluator_idea"]
    name = slug(idea["name"]).replace("-", "_")
    path = root / "evals" / "custom" / f"{name}.yaml"
    spec = {
        "name": name,
        "status": "draft",
        "criteria": idea["criteria"],
        "evaluation_params": idea["evaluation_params"],
        "threshold": 0.5,
        "source_issue": f"#{issue['id']} {issue['title']}",
        "rule_id": issue["rule_id"],
        "drafted_at": datetime.now(timezone.utc).date().isoformat(),
    }
    header = ("# Drafted by the improvement loop. Review the criteria, then set status: active\n"
              "# and the online evaluator will start scoring every request with it.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100),
                    encoding="utf-8", newline="\n")
    store.record_exit(issue["id"], "evaluator", f"evals/custom/{name}.yaml")
    return f"evals/custom/{name}.yaml"


# ---- exit 3: fix agent + pull request ------------------------------------------------
def _files_in_open_prs(store: IssueStore, skip_issue: int) -> dict[str, int]:
    files = {}
    for other in store.list_issues(("pr_open",)):
        pr = store.get_exit(other["id"], "pr")
        if other["id"] != skip_issue and pr:
            files.update({f: other["id"] for f in pr["detail"].get("files", [])})
    return files


def _github_env() -> dict:
    """Environment for git/gh that uses the chosen GitHub account without switching the
    user's active gh login."""
    env = dict(os.environ)
    if settings.GITHUB_ACCOUNT:
        token = subprocess.run(["gh", "auth", "token", "--user", settings.GITHUB_ACCOUNT],
                               capture_output=True, text=True, check=True).stdout.strip()
        env["GH_TOKEN"] = token
    return env


def pr_body(issue: dict, fix_summary: dict, tests_output: str, dataset: str | None,
            evaluator: str) -> str:
    evidence = "\n".join(f"| `{e['trace_id'][:16]}` | {e['observation']} |" for e in issue["evidence"])
    changes = "\n".join(f"- {c}" for c in fix_summary.get("changes", []))
    last_line = tests_output.strip().splitlines()[-1] if tests_output.strip() else "not run"
    return f"""## What the loop found
{issue['summary']}

**Broken rule:** `{issue['rule_id']}` ({issue['affected_requests']} of {issue['checked_requests']} \
requests in the investigated window){', related: ' + ', '.join(f'`{r}`' for r in issue['related_rules']) if issue['related_rules'] else ''}

### Evidence from production traces
| trace | observation |
|---|---|
{evidence}

### Root cause
{issue['root_cause']}

## The fix
{fix_summary.get('summary', '')}

{changes}

**Tests:** {last_line}

**Reviewer, please check:** {fix_summary.get('risks') or 'nothing flagged'}

## Regression set and evaluator
- Phoenix dataset `{dataset or 'n/a'}` holds the requests that broke the rule. \
`python -m loop verify {issue['id']}` replays them on `main` and on this branch.
- Draft evaluator `{evaluator}` (status: draft). Review it and set `status: active` to turn it on.

---
Opened by the improvement loop from production telemetry (issue #{issue['id']}). \
**It is never merged automatically. Please review before merging.**
"""


def open_pr(store: IssueStore, issue: dict, push: bool = True) -> str:
    if existing := store.get_exit(issue["id"], "pr"):
        return f"already has a PR: {existing['target']}"

    planned = {loc["path"] for loc in issue["code_locations"]}
    busy = _files_in_open_prs(store, issue["id"])
    if overlap := sorted(planned & busy.keys()):
        return (f"waiting: the open PR for issue #{busy[overlap[0]]} already changes {', '.join(overlap)}. "
                "Merge it and re-run the loop; that fix may change or resolve this issue.")

    branch = f"fix/issue-{issue['id']}-{slug(issue['rule_id'].split(':')[-1], 30)}"
    worktree = settings.WORKTREES_DIR / branch.replace("/", "-")
    if worktree.exists():
        git("worktree", "remove", "--force", str(worktree))
    if branch in git("branch", "--list", branch):
        git("branch", "-D", branch)
    git("worktree", "add", "-b", branch, str(worktree), settings.BASE_BRANCH)

    transcript = settings.TRANSCRIPTS_DIR / f"fix-issue-{issue['id']}.jsonl"
    result, ws, tests_output = run_fix(issue, worktree, transcript)
    if result.stop_reason != "finished":
        git("worktree", "remove", "--force", str(worktree))
        git("branch", "-D", branch)
        return f"fix agent gave up after {result.turns} turns (transcript: {transcript.name})"

    dataset = store.get_exit(issue["id"], "dataset")
    evaluator = draft_evaluator(store, issue, root=worktree)
    passed, tests_output = run_tests(worktree)
    files = sorted(ws.changed | {evaluator})
    git("add", "--", *files, cwd=worktree)
    message = (f"fix: {issue['title']}\n\n{result.output['summary']}\n\n"
               f"Found by the improvement loop (issue #{issue['id']}, rule {issue['rule_id']}).")
    git("commit", "-m", message, cwd=worktree)
    commit = git("rev-parse", "--short", "HEAD", cwd=worktree)
    body = pr_body(issue, result.output, tests_output, dataset and dataset["target"], evaluator)
    detail = {"branch": branch, "worktree": str(worktree), "commit": commit, "files": files,
              "tests_passed": passed, "fix_cost_usd": round(result.cost_usd, 4)}

    if push and settings.GITHUB_REPO:
        env = _github_env()
        url = f"https://github.com/{settings.GITHUB_REPO}.git"
        git("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", url, f"HEAD:refs/heads/{branch}", cwd=worktree, env=env)
        body_file = worktree.parent / f"{branch.replace('/', '-')}-body.md"
        body_file.write_text(body, encoding="utf-8")
        target = subprocess.run(
            ["gh", "pr", "create", "--repo", settings.GITHUB_REPO, "--base", settings.BASE_BRANCH,
             "--head", branch, "--title", f"fix: {issue['title']}", "--body-file", str(body_file)],
            cwd=worktree, env=env, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[-1]
    else:
        target = f"local branch {branch}"
        (worktree.parent / f"{branch.replace('/', '-')}-body.md").write_text(body, encoding="utf-8")

    store.record_exit(issue["id"], "pr", target, detail)
    store.set_status(issue["id"], "pr_open")
    return f"opened {target} (commit {commit}, fix agent cost about ${result.cost_usd:.2f})"


def fix_issue(issue_id: int, push: bool = True) -> list[str]:
    """Run every exit for one issue and report what happened."""
    from loop.agent import setup_loop_tracing

    setup_loop_tracing()
    store = IssueStore()
    issue = store.get_issue(issue_id)
    if issue is None:
        return [f"No issue #{issue_id}"]
    if issue["status"] in ("resolved", "muted"):
        return [f"Issue #{issue_id} is {issue['status']}; nothing to do."]
    lines = []
    try:
        lines.append(f"dataset:   {add_to_dataset(store, issue)}")
    except Exception as exc:  # the PR is still useful without a dataset
        lines.append(f"dataset:   skipped ({exc})")
    lines.append(f"pr:        {open_pr(store, issue, push=push)}")
    if evaluator := store.get_exit(issue_id, "evaluator"):
        lines.append(f"evaluator: {evaluator['target']} (draft, inside the PR)")
    return [f"Issue #{issue_id} {issue['title']}"] + [f"  {line}" for line in lines]


def fix_open_issues(push: bool = True) -> list[str]:
    store = IssueStore()
    lines = []
    for issue in sorted(store.list_issues(("open",)), key=lambda i: SEVERITY_ORDER[i["severity"]]):
        lines += fix_issue(issue["id"], push)
    return lines or ["No open issues to fix."]
