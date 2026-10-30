"""One investigation run: check the contract, let the agent explain the failures, save issues."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from loop import settings
from loop.agent import Tool, params, run_agent, setup_loop_tracing
from loop.contract import (CONTRACT_PATH, RuleResult, check_contract, format_report,
                           latency_breakdown, load_contract)
from loop.skills import InvestigationSkills, Snapshot
from loop.store import IssueStore
from loop.telemetry import fetch_requests
from loop.workspace import Workspace

INSTRUCTIONS = """You are an AI reliability engineer on call for Orbi, a retrieval-augmented (RAG) \
customer-support bot. A deterministic checker has tested recent production traces against the \
bot's health contract, and some rules failed. Explain each failed rule: find the root cause in \
the code, back it with trace evidence, and propose the smallest fix that would make it pass.

How to work:
1. For each failing rule, look for a pattern first. query_traces with group_by (for example \
app.channel or answer.confidence) and eval_summary show what the violating requests have in \
common compared with all requests.
2. Open two or three violating traces with get_trace to see exactly what happened.
3. Find the responsible code: map_span_to_code for the span involved, then search_code and \
read_file. Cite exact files and line numbers.
4. If several failing rules share one root cause, file one issue under the most severe rule \
and list the others in related_rules.
5. For each issue, propose a concrete minimal fix (what to change, in which file) and an \
LLM-judge evaluator that would catch the problem if it ever came back.

Rules:
- Cite only trace ids you have seen in tool results. Every evidence trace must be a request \
that actually broke that issue's rule. Your submission is verified automatically.
- Never propose weakening the health contract or the eval thresholds. Fix the application.
- The help-center articles in support_bot/kb/ are product facts owned by people; do not \
propose inventing new product facts.
- Be efficient: you have a limited number of tool calls.
- Finish by calling submit_findings exactly once."""

EVIDENCE = {"type": "object", "additionalProperties": False, "required": ["trace_id", "observation"],
            "properties": {"trace_id": {"type": "string"}, "observation": {"type": "string"}}}
CODE_LOCATION = {"type": "object", "additionalProperties": False, "required": ["path", "line", "why"],
                 "properties": {"path": {"type": "string"}, "line": {"type": "integer"},
                                "why": {"type": "string"}}}
EVALUATOR_IDEA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "criteria", "evaluation_params"],
    "properties": {
        "name": {"type": "string", "description": "snake_case metric name"},
        "criteria": {"type": "string", "description": "what an LLM judge should check"},
        "evaluation_params": {"type": "array", "items": {
            "type": "string", "enum": ["input", "actual_output", "retrieval_context"]}},
    },
}
ISSUE = {
    "type": "object", "additionalProperties": False,
    "required": ["rule_id", "title", "severity", "summary", "root_cause", "evidence",
                 "code_locations", "proposed_fix", "evaluator_idea", "related_rules"],
    "properties": {
        "rule_id": {"type": "string", "description": "the failing rule this issue explains"},
        "title": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "summary": {"type": "string", "description": "what is wrong and who it hurts, 2-4 sentences"},
        "root_cause": {"type": "string"},
        "evidence": {"type": "array", "items": EVIDENCE, "description": "2-6 violating traces"},
        "code_locations": {"type": "array", "items": CODE_LOCATION},
        "proposed_fix": {"type": "array", "items": {"type": "string"}},
        "evaluator_idea": EVALUATOR_IDEA,
        "related_rules": {"type": "array", "items": {"type": "string"},
                          "description": "other failing rule ids with the same root cause"},
    },
}
SUBMIT_FINDINGS = Tool(
    "submit_findings", "Submit one issue per root cause. Call once, at the end.",
    params(issues={"type": "array", "items": ISSUE}), run=lambda **_: "",
)


def validate_findings(args: dict, snap: Snapshot, ws: Workspace) -> str | None:
    """Reject findings that cite traces or code that do not back them up."""
    problems, covered = [], set()
    failing = {r.rule_id for r in snap.failing}
    for n, issue in enumerate(args.get("issues", []), start=1):
        rule, tag = issue["rule_id"], f"issue {n} ({issue['rule_id']})"
        if rule not in failing:
            problems.append(f"{tag}: rule_id must be one of the failing rules {sorted(failing)}")
            continue
        if rule in covered:
            problems.append(f"{tag}: another issue already covers this rule; merge them")
        covered.add(rule)
        covered.update(r for r in issue["related_rules"] if r in failing)
        if not 2 <= len(issue["evidence"]) <= 6:
            problems.append(f"{tag}: give between 2 and 6 evidence traces")
        for ev in issue["evidence"]:
            if ev["trace_id"] not in snap.by_id:
                problems.append(f"{tag}: trace {ev['trace_id']} does not exist in this snapshot")
            elif ev["trace_id"] not in snap.violations[rule]:
                problems.append(f"{tag}: trace {ev['trace_id']} did not break {rule}")
        if not issue["code_locations"]:
            problems.append(f"{tag}: cite at least one code location")
        for loc in issue["code_locations"]:
            try:
                path = ws.resolve(loc["path"])
            except PermissionError as exc:
                problems.append(f"{tag}: {exc}")
                continue
            if not path.is_file():
                problems.append(f"{tag}: {loc['path']} is not a file")
                continue
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if not 1 <= loc["line"] <= lines:
                problems.append(f"{tag}: {loc['path']} has {lines} lines, so line {loc['line']} is wrong")
        if not issue["proposed_fix"]:
            problems.append(f"{tag}: proposed_fix is empty")
    if missing := failing - covered:
        problems.append(f"These failing rules are not covered by any issue (file one, or list them "
                        f"in related_rules of the issue with the same root cause): {sorted(missing)}")
    return "\n".join(problems) or None


def build_task(snap: Snapshot) -> str:
    report = format_report(snap.contract, snap.requests, snap.results, snap.hours)
    failing = "\n".join(
        f"- {r.rule_id} [{r.severity}]: {r.detail}. Example traces: {', '.join(r.violations[:5])}"
        for r in snap.failing
    )
    latency = "\n".join(f"- {name}: p50 {s['p50_ms']:,.0f} ms, p95 {s['p95_ms']:,.0f} ms"
                        for name, s in latency_breakdown(snap.requests).items())
    return (f"Health contract (health_contract.yaml):\n```yaml\n"
            f"{CONTRACT_PATH.read_text(encoding='utf-8')}\n```\n\n"
            f"Checker report:\n{report}\n\nFailing rules:\n{failing}\n\n"
            f"Latency by span:\n{latency}\n\n"
            "Investigate every failing rule, then call submit_findings.")


@dataclass
class InvestigationOutcome:
    run_id: str
    snapshot: Snapshot
    saved: dict[str, list[str]] = field(default_factory=dict)  # outcome -> rule ids
    resolved: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    stop_reason: str = ""


def investigate(hours: float = 24, project: str = settings.APP_PROJECT,
                max_turns: int = 40) -> InvestigationOutcome:
    setup_loop_tracing()
    contract = load_contract()
    requests = fetch_requests(project, hours)
    snap = Snapshot(contract, requests, check_contract(contract, requests), hours)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    store = IssueStore()
    store.start_run(run_id, hours)
    outcome = InvestigationOutcome(run_id, snap)
    outcome.resolved = store.resolve_passing(snap.results)

    if len(requests) < contract.min_requests or not snap.failing:
        store.finish_run(run_id, status="done", requests=len(requests), failing_rules=[],
                         resolved_issues=len(outcome.resolved))
        outcome.stop_reason = "healthy" if snap.failing == [] else "not enough traffic"
        return outcome

    ws = Workspace(settings.REPO_ROOT)
    transcript = settings.TRANSCRIPTS_DIR / f"{run_id}.jsonl"
    result = run_agent(
        name="investigation", instructions=INSTRUCTIONS, task=build_task(snap),
        tools=InvestigationSkills(snap, ws).tools(), finish=SUBMIT_FINDINGS,
        validate=lambda args: validate_findings(args, snap, ws),
        transcript=transcript, max_turns=max_turns,
    )
    outcome.cost_usd, outcome.stop_reason = result.cost_usd, result.stop_reason

    rules = {r.rule_id: r for r in snap.results}
    for finding in (result.output or {}).get("issues", []):
        status = store.upsert_finding(contract.app, finding, rules[finding["rule_id"]], run_id)
        outcome.saved.setdefault(status, []).append(finding["rule_id"])

    store.finish_run(
        run_id, status="done" if result.output else "incomplete", requests=len(requests),
        failing_rules=[r.rule_id for r in snap.failing],
        new_issues=len(outcome.saved.get("new", [])) + len(outcome.saved.get("regressed", [])),
        updated_issues=len(outcome.saved.get("updated", [])), resolved_issues=len(outcome.resolved),
        model=settings.AGENT_MODEL, turns=result.turns, tool_calls=result.tool_calls,
        cost_usd=round(result.cost_usd, 4), transcript=str(transcript.relative_to(settings.REPO_ROOT)),
    )
    return outcome
