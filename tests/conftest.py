"""Shared test helpers. Every test runs offline: no Phoenix, no OpenAI."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from loop.telemetry import RequestTrace, SpanInfo

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_request(trace_id: str, spans=("input_guardrail", "retrieve", "generate_answer"),
                 duration_ms: float = 1000.0, evals: dict | None = None, **attributes) -> RequestTrace:
    """A fake customer request, shaped like the ones loop.telemetry builds from Phoenix."""
    attrs = {"app.channel": "web", "answer.blocked": False, "answer.answered": True,
             "answer.confidence": "high", "answer.citation_count": 1,
             "input.value": "How do I reset my password?", "output.value": "Use Forgot password."}
    attrs.update({k.replace("__", "."): v for k, v in attributes.items()})
    root = SpanInfo("support_request", f"{trace_id}-root", None, "CHAIN", T0, duration_ms, "OK", attrs)
    children = [SpanInfo(name, f"{trace_id}-{name}", root.span_id, "CHAIN", T0, 100.0, "OK", {})
                for name in spans]
    scores = {name: {"score": score, "label": "pass"} for name, score in (evals or {}).items()}
    return RequestTrace(trace_id, root, [root, *children], scores)


@pytest.fixture
def request_factory():
    return make_request
