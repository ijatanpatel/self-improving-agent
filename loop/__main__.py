"""Command line for the improvement loop.

    uv run python -m loop check            # test recent traffic against the health contract
    uv run python -m loop investigate      # agent explains failing rules, files issues
    uv run python -m loop issues           # list issues
    uv run python -m loop show 3           # one issue with its evidence
    uv run python -m loop fix 3            # dataset + evaluator draft + fix agent + pull request
    uv run python -m loop verify 3         # replay the failing questions on main vs. the fix
    uv run python -m loop run              # check, investigate, then fix what can be fixed
"""
from __future__ import annotations

import argparse
import textwrap

from loop import settings
from loop.contract import check_contract, format_report, load_contract
from loop.store import IssueStore
from loop.telemetry import fetch_requests


def cmd_check(args) -> None:
    contract = load_contract()
    requests = fetch_requests(args.project, args.hours)
    print(format_report(contract, requests, check_contract(contract, requests), args.hours))


def cmd_investigate(args) -> None:
    from loop.investigate import investigate

    outcome = investigate(args.hours, args.project, args.max_turns)
    print(format_report(outcome.snapshot.contract, outcome.snapshot.requests,
                        outcome.snapshot.results, args.hours))
    print()
    for issue in outcome.resolved:
        print(f"RESOLVED  #{issue['id']} {issue['title']}")
    for status, rules in outcome.saved.items():
        print(f"{status.upper():9s} {', '.join(rules)}")
    print(f"\nRun {outcome.run_id}: {outcome.stop_reason}; agent cost about ${outcome.cost_usd:.2f}")
    print("See the issues with: uv run python -m loop issues")


def cmd_issues(args) -> None:
    store = IssueStore()
    statuses = None if args.all else ("open", "pr_open")
    issues = store.list_issues(statuses)
    if not issues:
        print("No issues." if args.all else "No open issues. (--all shows resolved ones too)")
    for i in issues:
        pr = store.get_exit(i["id"], "pr")
        print(f"#{i['id']:<3} {i['status']:9s} {i['severity']:8s} {i['title']}")
        print(f"     rule {i['rule_id']}; {i['affected_requests']}/{i['checked_requests']} requests; "
              f"seen {i['times_seen']}x" + (f"; PR {pr['target']}" if pr else ""))


def cmd_show(args) -> None:
    store = IssueStore()
    issue = store.get_issue(args.issue_id)
    if issue is None:
        raise SystemExit(f"No issue #{args.issue_id}")
    wrap = lambda text: textwrap.fill(text, 100, initial_indent="  ", subsequent_indent="  ")
    print(f"#{issue['id']} [{issue['severity']}] {issue['title']}  ({issue['status']})")
    print(f"Rule: {issue['rule_id']}  related: {', '.join(issue['related_rules']) or 'none'}")
    print(f"Affected: {issue['affected_requests']}/{issue['checked_requests']} requests; "
          f"first seen {issue['first_seen']}, last seen {issue['last_seen']}\n")
    print("Summary:\n" + wrap(issue["summary"]))
    print("Root cause:\n" + wrap(issue["root_cause"]))
    print("Evidence:")
    for ev in issue["evidence"]:
        print(f"  - trace {ev['trace_id']}: {ev['observation']}")
    print("Code:")
    for loc in issue["code_locations"]:
        print(f"  - {loc['path']}:{loc['line']}  {loc['why']}")
    print("Proposed fix:")
    for n, step in enumerate(issue["proposed_fix"], 1):
        print(f"  {n}. {step}")
    idea = issue["evaluator_idea"]
    print(f"Evaluator idea: {idea['name']}: {idea['criteria']}")
    for ex in store.list_exits(issue["id"]):
        print(f"Exit: {ex['kind']} -> {ex['target']}")


def cmd_mute(args) -> None:
    IssueStore().set_status(args.issue_id, "muted")
    print(f"Issue #{args.issue_id} muted; re-runs will not reopen it.")


def cmd_fix(args) -> None:
    from loop.exits import fix_issue

    for line in fix_issue(args.issue_id, push=not args.no_pr):
        print(line)


def cmd_verify(args) -> None:
    from loop.verify import verify_issue

    print(verify_issue(args.issue_id, comment=args.comment))


def cmd_run(args) -> None:
    cmd_investigate(args)
    if args.fix:
        from loop.exits import fix_open_issues

        for line in fix_open_issues(push=True):
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m loop", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, window=False):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        if window:
            p.add_argument("--hours", type=float, default=24, help="how far back to look")
            p.add_argument("--project", default=settings.APP_PROJECT)
            p.add_argument("--max-turns", type=int, default=40)
        return p

    add("check", cmd_check, "check recent traffic against the health contract", window=True)
    add("investigate", cmd_investigate, "run the investigation agent", window=True)
    add("issues", cmd_issues, "list issues").add_argument("--all", action="store_true")
    for name, func, text in (("show", cmd_show, "show one issue"), ("mute", cmd_mute, "mute an issue")):
        add(name, func, text).add_argument("issue_id", type=int)
    verify = add("verify", cmd_verify, "before/after on the issue's regression set")
    verify.add_argument("issue_id", type=int)
    verify.add_argument("--comment", action="store_true", help="also post the table on the PR")
    fix = add("fix", cmd_fix, "dataset + evaluator draft + fix agent + pull request")
    fix.add_argument("issue_id", type=int)
    fix.add_argument("--no-pr", action="store_true", help="commit to a local branch only")
    run = add("run", cmd_run, "check, investigate, and optionally fix", window=True)
    run.add_argument("--fix", action="store_true", help="also open PRs for open issues")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
