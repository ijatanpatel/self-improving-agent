"""OpenTelemetry setup: every request becomes a trace in the local Phoenix server.

The app code only uses the plain OpenTelemetry API plus OpenInference attribute names,
so Phoenix could be swapped for any other OpenTelemetry backend without touching it.
"""
from __future__ import annotations

from openinference.instrumentation import TraceConfig
from openinference.instrumentation.openai import OpenAIInstrumentor
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from phoenix.otel import register

from support_bot import config

# Short aliases for the attribute names used on spans.
SPAN_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
INPUT = SpanAttributes.INPUT_VALUE
OUTPUT = SpanAttributes.OUTPUT_VALUE
CHAIN = OpenInferenceSpanKindValues.CHAIN.value
RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value
GUARDRAIL = OpenInferenceSpanKindValues.GUARDRAIL.value

# Until setup_tracing() runs, this tracer is a no-op, which keeps unit tests offline.
tracer = trace.get_tracer("support_bot")

_provider = None


def setup_tracing(project_name: str | None = None):
    """Send spans to Phoenix and auto-trace every OpenAI call (prompts, tokens, latency)."""
    global _provider
    if _provider is None:
        _provider = register(
            project_name=project_name or config.PHOENIX_PROJECT,
            endpoint=f"{config.PHOENIX_URL}/v1/traces",
            batch=True,
            verbose=False,
        )
        # Embedding vectors are large and useless to read, so keep them out of the traces.
        OpenAIInstrumentor().instrument(
            tracer_provider=_provider, config=TraceConfig(hide_embedding_vectors=True)
        )
    return _provider


def flush() -> None:
    """Push any buffered spans to Phoenix (call before a script exits)."""
    if _provider is not None:
        _provider.force_flush()


def current_trace_id(span) -> str:
    return format(span.get_span_context().trace_id, "032x")
