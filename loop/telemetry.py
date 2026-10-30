"""Read traces and eval scores back out of Phoenix as one record per customer request."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from phoenix.client import Client

PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
ROOT_SPAN = "support_request"
_DOC_KEY = re.compile(r"^retrieval\.documents\.(\d+)\.document\.(id|content|score)$")


@dataclass
class SpanInfo:
    name: str
    span_id: str
    parent_id: str | None
    kind: str
    start: datetime
    duration_ms: float
    status: str
    attributes: dict


@dataclass
class RequestTrace:
    """One customer request: the root span, its child spans and its eval scores."""
    trace_id: str
    root: SpanInfo
    spans: list[SpanInfo]
    evals: dict[str, dict] = field(default_factory=dict)  # name -> {score, label, explanation}

    @property
    def question(self) -> str:
        return str(self.root.attributes.get("input.value", ""))

    @property
    def answer(self) -> str:
        return str(self.root.attributes.get("output.value", ""))

    def span(self, name: str) -> SpanInfo | None:
        return next((s for s in self.spans if s.name == name), None)

    def documents(self) -> list[dict]:
        """Help-center chunks the retrieve span returned, best first."""
        retrieve = self.span("retrieve")
        if retrieve is None:
            return []
        attrs = retrieve.attributes
        if isinstance(attrs.get("retrieval.documents"), list):  # nested form
            return [
                {"id": d.get("document.id"), "content": d.get("document.content"),
                 "score": d.get("document.score")}
                for d in attrs["retrieval.documents"]
            ]
        docs: dict[int, dict] = {}
        for key, value in attrs.items():  # flattened form: retrieval.documents.0.document.id
            match = _DOC_KEY.match(key)
            if match:
                docs.setdefault(int(match.group(1)), {})[match.group(2)] = value
        return [docs[i] for i in sorted(docs)]

    def fields(self) -> dict:
        """Flat view used by the health contract: root attributes, eval scores and timings."""
        values = {k: v for k, v in self.root.attributes.items() if not k.startswith("retrieval.")}
        values["duration_ms"] = self.root.duration_ms
        for span in self.spans:
            values.setdefault(f"span.{span.name}.duration_ms", span.duration_ms)
        for name, result in self.evals.items():
            values[f"eval.{name}"] = result.get("score")
        return values


def client() -> Client:
    return Client(base_url=PHOENIX_URL)


def _to_span(raw: dict) -> SpanInfo:
    start = datetime.fromisoformat(raw["start_time"])
    end = datetime.fromisoformat(raw["end_time"])
    return SpanInfo(
        name=raw["name"],
        span_id=raw["context"]["span_id"],
        parent_id=raw.get("parent_id"),
        kind=raw.get("span_kind", ""),
        start=start,
        duration_ms=(end - start).total_seconds() * 1000,
        status=raw.get("status_code", ""),
        attributes=raw.get("attributes") or {},
    )


def fetch_annotations(project: str, span_ids: list[str]) -> dict[str, dict[str, dict]]:
    """span_id -> {annotation name -> {score, label, explanation}}."""
    out: dict[str, dict[str, dict]] = {}
    for i in range(0, len(span_ids), 100):  # keep each request URL short
        batch = span_ids[i:i + 100]
        for ann in client().spans.get_span_annotations(
            span_ids=batch, project_identifier=project, limit=10_000, timeout=60
        ):
            out.setdefault(ann["span_id"], {})[ann["name"]] = dict(ann.get("result") or {})
    return out


def fetch_requests(project: str, hours: float = 24, until: datetime | None = None) -> list[RequestTrace]:
    """All customer requests in the last `hours`, oldest first, with their eval scores."""
    until = until or datetime.now(timezone.utc)
    raw_spans = client().spans.get_spans(
        project_identifier=project,
        start_time=until - timedelta(hours=hours),
        end_time=until,
        limit=100_000,
        timeout=120,
    )
    by_trace: dict[str, list[SpanInfo]] = {}
    for raw in raw_spans:
        by_trace.setdefault(raw["context"]["trace_id"], []).append(_to_span(raw))

    requests = []
    for trace_id, spans in by_trace.items():
        root = next((s for s in spans if s.name == ROOT_SPAN and not s.parent_id), None)
        if root is None:
            continue  # a stray span from a crashed process; not a full request
        spans.sort(key=lambda s: s.start)
        requests.append(RequestTrace(trace_id, root, spans))

    annotations = fetch_annotations(project, [r.root.span_id for r in requests])
    for request in requests:
        request.evals = annotations.get(request.root.span_id, {})
    return sorted(requests, key=lambda r: r.root.start)
