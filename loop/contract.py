"""The health contract: a YAML file that says what "healthy" means for the bot, plus a
checker that tests every recent request against it.

The checker is plain code, not an LLM, so its verdicts are fast, free and repeatable.
The investigation agent is only asked to explain failures that this checker has proven.
"""
from __future__ import annotations

import math
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

from loop.telemetry import RequestTrace

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "health_contract.yaml"
Severity = Literal["low", "medium", "high", "critical"]

_COMPARISON = re.compile(r"^\s*([\w.]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
_OPERATORS = {
    "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
    "<=": operator.le, ">": operator.gt, "<": operator.lt,
}


class ContractError(ValueError):
    pass


def _parse_value(text: str):
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        return text[1:-1]
    try:
        return float(text)
    except ValueError:
        raise ContractError(f"Cannot read value {text!r}; put strings in quotes, like 'high'")


@dataclass(frozen=True)
class Condition:
    """One comparison such as `eval.faithfulness >= 0.7`. Deliberately not Python eval()."""
    field: str
    op: str
    value: object

    @classmethod
    def parse(cls, text: str) -> "Condition":
        match = _COMPARISON.match(text)
        if not match:
            raise ContractError(f"Cannot read condition {text!r}; expected '<field> <op> <value>'")
        name, op, raw = match.groups()
        return cls(name, op, _parse_value(raw))

    def test(self, fields: dict) -> bool | None:
        """True or False, or None when the field is missing (for example, not scored yet)."""
        actual = fields.get(self.field)
        if actual is None:
            return None
        try:
            return _OPERATORS[self.op](actual, self.value)
        except TypeError:
            return None

    def __str__(self) -> str:
        value = f"'{self.value}'" if isinstance(self.value, str) else str(self.value).lower()
        return f"{self.field} {self.op} {value}"


def _all_hold(conditions: list[Condition], fields: dict) -> bool | None:
    results = [c.test(fields) for c in conditions]
    if False in results:
        return False
    return None if None in results else True


def _as_list(value):
    return [value] if isinstance(value, str) else value or []


class RequiredSpan(BaseModel):
    span: str
    description: str = ""
    when: list[str] = []
    severity: Severity = "high"

    @field_validator("when", mode="before")
    @classmethod
    def accept_single_condition(cls, value):
        return _as_list(value)


class Invariant(BaseModel):
    id: str
    description: str
    when: list[str] = []
    require: list[str]
    severity: Severity = "medium"
    # Error budget: the share of requests allowed to break the rule. Rules scored by an
    # LLM judge need one, because even a good judge is sometimes wrong.
    max_violation_rate: float = 0.0

    @field_validator("when", "require", mode="before")
    @classmethod
    def accept_single_condition(cls, value):
        return _as_list(value)


class LatencySLO(BaseModel):
    span: str
    percentile: float = 95
    max_ms: float
    severity: Severity = "medium"


class HealthContract(BaseModel):
    app: str
    phoenix_project: str
    root_span: str
    min_requests: int = 20
    required_spans: list[RequiredSpan] = []
    invariants: list[Invariant] = []
    latency_slo: LatencySLO | None = None

    def model_post_init(self, _context) -> None:
        for rule in self.required_spans:  # fail fast on typos in any condition
            [Condition.parse(c) for c in rule.when]
        for rule in self.invariants:
            [Condition.parse(c) for c in rule.when + rule.require]


def load_contract(path: Path = CONTRACT_PATH) -> HealthContract:
    return HealthContract(**yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass
class RuleResult:
    rule_id: str          # stable id, e.g. "invariant:answers_are_grounded"
    kind: str             # required_span | invariant | latency_slo
    description: str
    severity: str
    checked: int = 0      # requests the rule applied to
    skipped: int = 0      # rule applied, but data was missing (e.g. not scored yet)
    violations: list[str] = field(default_factory=list)  # trace ids that broke the rule
    budget: float = 0.0   # allowed violation rate (error budget)
    detail: str = ""

    @property
    def rate(self) -> float:
        scored = self.checked - self.skipped
        return len(self.violations) / scored if scored else 0.0

    @property
    def failed(self) -> bool:
        return bool(self.violations) and self.rate > self.budget


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile: the value below which pct% of the observations fall."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _span_duration(request: RequestTrace, span_name: str) -> float | None:
    span = request.root if request.root.name == span_name else request.span(span_name)
    return span.duration_ms if span else None


def check_contract(contract: HealthContract, requests: list[RequestTrace]) -> list[RuleResult]:
    results = []
    fields = {r.trace_id: r.fields() for r in requests}

    for rule in contract.required_spans:
        result = RuleResult(f"required_span:{rule.span}", "required_span",
                            rule.description or f"every request must contain a '{rule.span}' span",
                            rule.severity)
        when = [Condition.parse(c) for c in rule.when]
        for r in requests:
            if when and not _all_hold(when, fields[r.trace_id]):
                continue
            result.checked += 1
            if r.span(rule.span) is None:
                result.violations.append(r.trace_id)
        result.detail = f"span '{rule.span}' missing in {len(result.violations)}/{result.checked} requests"
        results.append(result)

    for rule in contract.invariants:
        result = RuleResult(f"invariant:{rule.id}", "invariant", rule.description, rule.severity,
                            budget=rule.max_violation_rate)
        when = [Condition.parse(c) for c in rule.when]
        require = [Condition.parse(c) for c in rule.require]
        for r in requests:
            if when and not _all_hold(when, fields[r.trace_id]):
                continue
            result.checked += 1
            verdict = _all_hold(require, fields[r.trace_id])
            if verdict is None:
                result.skipped += 1
            elif verdict is False:
                result.violations.append(r.trace_id)
        condition = " and ".join(str(c) for c in require)
        scope = f"when {' and '.join(str(c) for c in when)}, " if when else ""
        result.detail = (f"{scope}require {condition}: broken in "
                         f"{len(result.violations)}/{result.checked - result.skipped} scored requests "
                         f"({result.rate:.0%}; budget {result.budget:.0%})")
        results.append(result)

    slo = contract.latency_slo
    if slo:
        result = RuleResult(f"latency_slo:{slo.span}", "latency_slo",
                            f"p{slo.percentile:g} latency of '{slo.span}' must stay under {slo.max_ms:,.0f} ms",
                            slo.severity)
        timed = [(r.trace_id, d) for r in requests if (d := _span_duration(r, slo.span)) is not None]
        result.checked = len(timed)
        if timed:
            observed = percentile([d for _, d in timed], slo.percentile)
            if observed > slo.max_ms:  # only the slow requests count as evidence
                result.violations = [tid for tid, d in timed if d > slo.max_ms]
            result.detail = (f"p{slo.percentile:g} = {observed:,.0f} ms (limit {slo.max_ms:,.0f} ms); "
                             f"{sum(d > slo.max_ms for _, d in timed)} requests over the limit")
        results.append(result)
    return results


def latency_breakdown(requests: list[RequestTrace]) -> dict[str, dict[str, float]]:
    """p50/p95 duration for every span name, to show where the time goes."""
    durations: dict[str, list[float]] = {}
    for r in requests:
        for span in r.spans:
            durations.setdefault(span.name, []).append(span.duration_ms)
    return {
        name: {"count": len(v), "p50_ms": percentile(v, 50), "p95_ms": percentile(v, 95)}
        for name, v in sorted(durations.items())
    }


def format_report(contract: HealthContract, requests: list[RequestTrace],
                  results: list[RuleResult], hours: float) -> str:
    versions = sorted({str(r.root.attributes.get("app.version", "?")) for r in requests})
    lines = [
        f"Health check for '{contract.phoenix_project}': {len(requests)} requests in the last "
        f"{hours:g}h (app versions: {', '.join(versions) or 'none'})",
        "",
    ]
    if len(requests) < contract.min_requests:
        lines.append(f"Not enough traffic to judge (need at least {contract.min_requests} requests).")
    for res in results:
        status = "FAIL" if res.failed else "PASS"
        lines.append(f"{status}  [{res.severity:6s}] {res.rule_id}")
        lines.append(f"      {res.detail}" + (f"; {res.skipped} not scored yet" if res.skipped else ""))
    return "\n".join(lines)
