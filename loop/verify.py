"""Before/after check for one issue: replay its regression set on main and on the fix.

Both versions answer the same saved questions, each into its own Phoenix project. The
same online evaluator scores both and the same health contract is checked on both, so
the pull request gets measured numbers instead of a promise.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from loop import settings
from loop.contract import check_contract, load_contract, percentile
from loop.exits import _github_env, git
from loop.store import IssueStore
from loop.telemetry import client, fetch_requests

EVAL_NAMES = ("faithfulness", "context_sufficiency", "contextual_relevancy")


def _replay(worktree: Path, questions: Path, project: str) -> None:
    """Run the bot from that worktree's code (cwd decides which support_bot is imported)."""
    proc = subprocess.run(
        [sys.executable, "-m", "support_bot.replay", "--file", str(questions), "--project", project],
        cwd=worktree, capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"replay failed in {worktree.name}:\n{proc.stderr[-2000:]}")


def _wait_for_traces(project: str, expected: int, timeout_s: int = 90) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and len(fetch_requests(project, hours=3)) < expected:
        time.sleep(3)


def summarize(project: str) -> dict:
    requests = fetch_requests(project, hours=3)
    results = check_contract(load_contract(), requests)
    durations = [r.root.duration_ms for r in requests]

    def avg(name: str) -> float | None:
        scores = [r.evals[name]["score"] for r in requests if r.evals.get(name, {}).get("score") is not None]
        return mean(scores) if scores else None

    return {
        "requests": len(requests),
        "rules": {res.rule_id: {"violations": len(res.violations), "scored": res.checked - res.skipped,
                                "failed": res.failed} for res in results},
        "evals": {name: avg(name) for name in EVAL_NAMES},
        "p50_ms": percentile(durations, 50) if durations else None,
        "p95_ms": percentile(durations, 95) if durations else None,
        "answered": sum(bool(r.root.attributes.get("answer.answered")) for r in requests),
        "high_confidence": sum(r.root.attributes.get("answer.confidence") == "high" for r in requests),
    }


def format_report(issue: dict, before: dict, after: dict, branch: str) -> str:
    def num(value, fmt="{:.2f}"):
        return "-" if value is None else fmt.format(value)

    rows = []
    for rule, b in before["rules"].items():
        a = after["rules"].get(rule, {"violations": 0, "scored": 0, "failed": False})
        if b["violations"] or a["violations"]:
            mark = lambda x: " (fails)" if x["failed"] else ""
            rows.append(f"| `{rule}` broken | {b['violations']}/{b['scored']}{mark(b)} | "
                        f"{a['violations']}/{a['scored']}{mark(a)} |")
    for name in EVAL_NAMES:
        rows.append(f"| mean {name} | {num(before['evals'][name])} | {num(after['evals'][name])} |")
    rows.append(f"| latency p50 / p95 | {num(before['p50_ms'], '{:,.0f}')} / {num(before['p95_ms'], '{:,.0f}')} ms | "
                f"{num(after['p50_ms'], '{:,.0f}')} / {num(after['p95_ms'], '{:,.0f}')} ms |")
    rows.append(f"| answered / high confidence | {before['answered']} / {before['high_confidence']} | "
                f"{after['answered']} / {after['high_confidence']} |")
    return (f"## Before / after: issue #{issue['id']}\n"
            f"Replayed the {before['requests']} requests from the issue's regression set on "
            f"`{settings.BASE_BRANCH}` (before) and `{branch}` (after), scored both with the same "
            f"online evaluator, and checked both against the same health contract.\n\n"
            "| | before | after |\n|---|---|---|\n" + "\n".join(rows) + "\n")


def verify_issue(issue_id: int, comment: bool = False) -> str:
    store = IssueStore()
    issue = store.get_issue(issue_id)
    pr, dataset = store.get_exit(issue_id, "pr"), store.get_exit(issue_id, "dataset")
    if issue is None or pr is None or dataset is None:
        return "Issue needs a regression dataset and a fix branch first: python -m loop fix <id>"

    examples = client().datasets.get_dataset(dataset=dataset["target"]).examples
    stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    out_dir = settings.REPORTS_DIR / f"issue-{issue_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    questions = out_dir / "regression-set.jsonl"
    questions.write_text("\n".join(json.dumps(ex["input"]) for ex in examples), encoding="utf-8")

    branch = pr["detail"]["branch"]
    fix_tree = Path(pr["detail"]["worktree"])
    if not fix_tree.exists():
        git("worktree", "add", str(fix_tree), branch)
    base_tree = settings.WORKTREES_DIR / f"base-issue-{issue_id}"
    if base_tree.exists():
        git("worktree", "remove", "--force", str(base_tree))
    git("worktree", "add", "--detach", str(base_tree), settings.BASE_BRANCH)

    from evals.online_evaluator import evaluate_once  # imported late: loads DeepEval

    summaries = {}
    try:
        for label, tree in (("before", base_tree), ("after", fix_tree)):
            project = f"verify-{issue_id}-{label}-{stamp}"
            _replay(tree, questions, project)
            _wait_for_traces(project, len(examples))
            asyncio.run(evaluate_once(project, hours=3, limit=1000, concurrency=8))
            summaries[label] = summarize(project)
    finally:
        git("worktree", "remove", "--force", str(base_tree))

    report = format_report(issue, summaries["before"], summaries["after"], branch)
    (out_dir / f"before-after-{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / f"before-after-{stamp}.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    if comment and pr["target"].startswith("https://"):
        subprocess.run(["gh", "pr", "comment", pr["target"], "--body", report],
                       env=_github_env(), check=True, capture_output=True, text=True)
        report += f"\nPosted as a comment on {pr['target']}\n"
    return report
