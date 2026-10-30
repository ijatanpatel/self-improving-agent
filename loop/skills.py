"""Investigation skills: small read-only tools the investigating agent can call.

Each skill answers one question with a short text summary, never a raw data dump, so
the agent's context stays small. All of them work on one snapshot of the traces taken
when the run starts, so every number the agent sees is consistent with the checker.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean

from loop.agent import Tool, params
from loop.contract import (Condition, HealthContract, RuleResult, _all_hold, latency_breakdown,
                           percentile)
from loop.telemetry import RequestTrace
from loop.workspace import Workspace

FIELDS_HELP = (
    "Fields: app.channel, app.version, answer.confidence, answer.answered, answer.blocked, "
    "answer.citation_count, eval.faithfulness, eval.context_sufficiency, "
    "eval.contextual_relevancy, duration_ms, span.<span name>.duration_ms"
)
SPAN_CALL = r"start_as_current_span\(\s*[\"']{name}[\"']"


@dataclass
class Snapshot:
    """Everything one investigation run knows about production, fetched once."""
    contract: HealthContract
    requests: list[RequestTrace]
    results: list[RuleResult]
    hours: float
    by_id: dict = field(init=False)
    fields: dict = field(init=False)
    violations: dict = field(init=False)

    def __post_init__(self):
        self.by_id = {r.trace_id: r for r in self.requests}
        self.fields = {r.trace_id: r.fields() for r in self.requests}
        self.violations = {res.rule_id: set(res.violations) for res in self.results}

    @property
    def failing(self) -> list[RuleResult]:
        return [r for r in self.results if r.failed]


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return "-" if value is None else str(value)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


class InvestigationSkills:
    def __init__(self, snapshot: Snapshot, workspace: Workspace):
        self.snap = snapshot
        self.ws = workspace

    def _missing_spans(self, request: RequestTrace) -> list[str]:
        return [s.span for s in self.snap.contract.required_spans if request.span(s.span) is None]

    def _broken_rules(self, trace_id: str) -> list[str]:
        return [rule for rule, ids in self.snap.violations.items() if trace_id in ids]

    # ---- skill: fetch traces ---------------------------------------------------------
    def query_traces(self, rule_id: str | None, where: str | None, group_by: str | None,
                     limit: int | None) -> str:
        selected, label = self.snap.requests, "requests"
        if rule_id:
            if rule_id not in self.snap.violations:
                return f"Unknown rule_id {rule_id!r}. Known rules: {sorted(self.snap.violations)}"
            selected = [r for r in selected if r.trace_id in self.snap.violations[rule_id]]
            label = f"requests that broke {rule_id}"
        if where:
            conditions = [Condition.parse(part) for part in re.split(r"\s+and\s+", where)]
            selected = [r for r in selected if _all_hold(conditions, self.snap.fields[r.trace_id])]
            label += f" where {where}"

        if group_by:
            matching = Counter(_fmt(self.snap.fields[r.trace_id].get(group_by)) for r in selected)
            everyone = Counter(_fmt(f.get(group_by)) for f in self.snap.fields.values())
            lines = [f"{len(selected)} {label}, grouped by {group_by} "
                     f"(compared with all {len(self.snap.requests)} requests):",
                     "value | matching | all requests | share of that value that matches"]
            for value, total in sorted(everyone.items(), key=lambda kv: -matching[kv[0]]):
                lines.append(f"{value} | {matching[value]} | {total} | {matching[value] / total:.0%}")
            return "\n".join(lines)

        limit = max(1, min(limit or 8, 20))
        lines = [f"{len(selected)} {label}; showing {min(limit, len(selected))}:"]
        for r in selected[:limit]:
            f = self.snap.fields[r.trace_id]
            missing = ",".join(self._missing_spans(r)) or "none"
            lines.append(
                f"- {r.trace_id} | {f.get('app.channel')} | confidence={_fmt(f.get('answer.confidence'))} "
                f"answered={_fmt(f.get('answer.answered'))} blocked={_fmt(f.get('answer.blocked'))} "
                f"citations={_fmt(f.get('answer.citation_count'))} | "
                f"faithfulness={_fmt(f.get('eval.faithfulness'))} "
                f"sufficiency={_fmt(f.get('eval.context_sufficiency'))} "
                f"relevancy={_fmt(f.get('eval.contextual_relevancy'))} | {r.root.duration_ms:,.0f} ms | "
                f"missing spans: {missing}\n  Q: {_clip(r.question, 130)}\n  A: {_clip(r.answer, 160)}"
            )
        return "\n".join(lines)

    def get_trace(self, trace_id: str) -> str:
        r = self.snap.by_id.get(trace_id)
        if r is None:
            return f"No request with trace id {trace_id} in this snapshot."
        a = r.root.attributes
        lines = [
            f"Trace {trace_id} ({r.root.start:%Y-%m-%d %H:%M:%S} UTC, {r.root.duration_ms:,.0f} ms)",
            f"channel={a.get('app.channel')} version={a.get('app.version')} "
            f"confidence={a.get('answer.confidence')} answered={a.get('answer.answered')} "
            f"blocked={a.get('answer.blocked')} citations={a.get('answer.citation_count')}",
            f"Breaks rules: {', '.join(self._broken_rules(trace_id)) or 'none'}",
            f"Missing required spans: {', '.join(self._missing_spans(r)) or 'none'}",
            f"Question (as recorded on the trace): {_clip(r.question, 700)}",
            f"Answer: {_clip(r.answer, 900)}",
            "Spans (in order):",
        ]
        for s in r.spans:
            keys = [k for k in s.attributes if k.startswith(("guardrail.", "retrieval.top", "llm.model_name",
                                                            "llm.token_count.prompt", "llm.token_count.completion"))]
            extra = " ".join(f"{k}={_fmt(s.attributes[k])}" for k in sorted(keys))
            lines.append(f"  {s.name} [{s.kind}] {s.duration_ms:,.0f} ms {extra}".rstrip())
        docs = r.documents()
        if docs:
            lines.append(f"Retrieved {len(docs)} chunks (id, similarity):")
            lines += [f"  {d['id']} ({_fmt(float(d['score']))}): {_clip(str(d['content']), 90)}" for d in docs]
        for name, ev in sorted(r.evals.items()):
            lines.append(f"Eval {name}: score={_fmt(ev.get('score'))} label={ev.get('label')} "
                         f"judge says: {_clip(ev.get('explanation') or '', 350)}")
        return "\n".join(lines)

    # ---- skill: fetch evals ----------------------------------------------------------
    def eval_summary(self, group_by: str | None) -> str:
        names = sorted({n for r in self.snap.requests for n in r.evals})
        if not names:
            return "No eval scores yet. The online evaluator may not have run."
        lines = []
        for name in names:
            groups: dict[str, list[dict]] = {}
            for r in self.snap.requests:
                if name in r.evals:
                    key = _fmt(self.snap.fields[r.trace_id].get(group_by)) if group_by else "all"
                    groups.setdefault(key, []).append(r.evals[name])
            lines.append(f"{name}:" + (f" (by {group_by})" if group_by else ""))
            for key, evs in sorted(groups.items()):
                scores = [e["score"] for e in evs if e.get("score") is not None]
                passed = sum(e.get("label") == "pass" for e in evs)
                lines.append(f"  {key}: n={len(evs)} mean={mean(scores):.2f} pass={passed}/{len(evs)}")

        # The cross-tab that matters most: stated confidence vs. whether retrieval found the answer.
        cross = Counter()
        for r in self.snap.requests:
            f = self.snap.fields[r.trace_id]
            suff = f.get("eval.context_sufficiency")
            if suff is not None and f.get("answer.confidence"):
                cross[(f["answer.confidence"], "found answer" if suff >= 0.5 else "did NOT find answer")] += 1
        if cross:
            lines.append("Stated confidence vs. retrieval (context_sufficiency >= 0.5):")
            lines += [f"  confidence={c} & retrieval {s}: {n}" for (c, s), n in sorted(cross.items())]
        return "\n".join(lines)

    def latency_breakdown(self, group_by: str | None) -> str:
        lines = ["Span durations (p50 / p95 ms, count):"]
        for name, s in latency_breakdown(self.snap.requests).items():
            lines.append(f"  {name}: {s['p50_ms']:,.0f} / {s['p95_ms']:,.0f} (n={s['count']})")
        if group_by:
            groups: dict[str, list[float]] = {}
            for r in self.snap.requests:
                groups.setdefault(_fmt(self.snap.fields[r.trace_id].get(group_by)), []).append(r.root.duration_ms)
            lines.append(f"Whole-request latency by {group_by} (p50 / p95 ms):")
            lines += [f"  {k}: {percentile(v, 50):,.0f} / {percentile(v, 95):,.0f} (n={len(v)})"
                      for k, v in sorted(groups.items())]
        return "\n".join(lines)

    # ---- skill: map span to code -----------------------------------------------------
    def map_span_to_code(self, span_name: str) -> str:
        hits = self.ws.search_code(SPAN_CALL.format(name=re.escape(span_name)), "*.py")
        if hits == "No matches.":
            return f"No code creates a span named {span_name!r}."
        lines = [f"Span {span_name!r} is created at:"]
        for hit in hits.splitlines():
            path, line_no, _ = hit.split(":", 2)
            source = (self.ws.root / path).read_text(encoding="utf-8").splitlines()
            func = next((m.group(1) for i in range(int(line_no) - 1, -1, -1)
                         if (m := re.match(r"\s*def (\w+)\(", source[i]))), None)
            lines.append(f"  {path}:{line_no} inside function {func}()")
            if func:
                callers = [c for c in self.ws.search_code(rf"\b{func}\(", "*.py").splitlines()
                           if f"def {func}(" not in c and not c.startswith("tests/")]
                lines.append(f"  Called from ({len(callers)}):")
                lines += [f"    {c}" for c in callers] or ["    (no callers found)"]
        return "\n".join(lines)

    # ---- tool list -------------------------------------------------------------------
    def tools(self) -> list[Tool]:
        nullable_str = {"type": ["string", "null"]}
        return [
            Tool("query_traces",
                 "Find requests in the snapshot. Filter by a broken rule (rule_id) and/or a condition "
                 "like \"app.channel == 'email' and eval.faithfulness < 0.7\". With group_by, returns "
                 "counts per value compared with all requests (the fastest way to spot a pattern). "
                 "Without group_by, returns example rows. " + FIELDS_HELP,
                 params(rule_id=nullable_str, where=nullable_str, group_by=nullable_str,
                        limit={"type": ["integer", "null"], "description": "rows to show, max 20"}),
                 self.query_traces),
            Tool("get_trace",
                 "Full detail of one request: question, answer, every span with timing, retrieved "
                 "chunks with similarity scores, and each eval score with the judge's explanation.",
                 params(trace_id={"type": "string"}), self.get_trace),
            Tool("eval_summary",
                 "Mean score and pass rate of each online eval, optionally grouped by a field "
                 "(e.g. answer.confidence or app.channel), plus confidence vs. retrieval quality.",
                 params(group_by=nullable_str), self.eval_summary),
            Tool("latency_breakdown",
                 "p50/p95 duration of every span name, optionally whole-request latency by a field.",
                 params(group_by=nullable_str), self.latency_breakdown),
            Tool("map_span_to_code",
                 "Find the file, line and function that creates a span, and every place that "
                 "function is called from. Use it to go from a trace to the responsible code.",
                 params(span_name={"type": "string"}), self.map_span_to_code),
            Tool("read_file",
                 "Read a repository file with line numbers (max 250 lines per call).",
                 params(path={"type": "string"}, start_line={"type": ["integer", "null"]},
                        end_line={"type": ["integer", "null"]}), self.ws.read_file),
            Tool("search_code",
                 "Regex search over repository files; returns path:line: text. "
                 "Optional glob such as 'support_bot/*.py'.",
                 params(pattern={"type": "string"}, glob=nullable_str), self.ws.search_code),
            Tool("list_files", "List repository files under a directory ('.' for all).",
                 params(directory={"type": "string"}), self.ws.list_files),
        ]
