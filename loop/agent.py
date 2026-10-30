"""A small tool-calling agent loop on the OpenAI Responses API.

Each turn the model either calls tools (we run them and send back the results) or calls
the finish tool. Our own code checks the finish tool's input; if the check fails, the
problems go back to the model so it can fix them and try again. Every step is written
to a JSONL transcript and traced into Phoenix, so the agent is observable too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from openai import BadRequestError, OpenAI
from openinference.instrumentation.openai import OpenAIInstrumentor
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from phoenix.otel import register

from loop import settings
from loop.telemetry import PHOENIX_URL

MAX_TOOL_OUTPUT = 8000  # characters; one huge result must not flood the agent's context
KIND, INPUT, OUTPUT = (SpanAttributes.OPENINFERENCE_SPAN_KIND, SpanAttributes.INPUT_VALUE,
                       SpanAttributes.OUTPUT_VALUE)
tracer = trace.get_tracer("improvement_loop")
_tracing_ready = False


def setup_loop_tracing() -> None:
    """Trace the agents themselves into their own Phoenix project."""
    global _tracing_ready
    if not _tracing_ready:
        provider = register(project_name=settings.LOOP_PROJECT, endpoint=f"{PHOENIX_URL}/v1/traces",
                            batch=True, verbose=False)
        OpenAIInstrumentor().instrument(tracer_provider=provider)
        _tracing_ready = True


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., str]

    def schema(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": self.parameters, "strict": True}


def params(**properties: dict) -> dict:
    """JSON schema in OpenAI strict mode: every property listed and required, nothing extra.
    Optional values are declared as nullable, e.g. {"type": ["string", "null"]}."""
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


@dataclass
class AgentResult:
    output: dict | None = None
    stop_reason: str = "max_turns"
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def _add_usage(result: AgentResult, response, model: str) -> None:
    usage = response.usage
    if usage is None:
        return
    cached = usage.input_tokens_details.cached_tokens if usage.input_tokens_details else 0
    result.input_tokens += usage.input_tokens
    result.cached_tokens += cached
    result.output_tokens += usage.output_tokens
    price_in, price_cached, price_out = settings.PRICES.get(model, (0.0, 0.0, 0.0))
    result.cost_usd += ((usage.input_tokens - cached) * price_in + cached * price_cached
                        + usage.output_tokens * price_out) / 1_000_000


def _run_tool(tool: Tool, args: dict) -> str:
    with tracer.start_as_current_span(
        f"tool.{tool.name}",
        attributes={KIND: OpenInferenceSpanKindValues.TOOL.value, INPUT: json.dumps(args)},
    ) as span:
        try:
            output = tool.run(**args)
        except Exception as exc:  # report the error to the model instead of crashing
            output = f"ERROR: {type(exc).__name__}: {exc}"
        if len(output) > MAX_TOOL_OUTPUT:
            output = (output[:MAX_TOOL_OUTPUT]
                      + f"\n... [cut {len(output) - MAX_TOOL_OUTPUT} characters; narrow your query]")
        span.set_attribute(OUTPUT, output)
        return output


def run_agent(*, name: str, instructions: str, task: str, tools: list[Tool], finish: Tool,
              validate: Callable[[dict], str | None], transcript: Path, max_turns: int = 30,
              model: str = settings.AGENT_MODEL, effort: str = settings.AGENT_EFFORT) -> AgentResult:
    """Run until the finish tool is called with input that passes validate(), or max_turns.

    validate(args) returns None when the finish call is acceptable, else an error message.
    """
    client = OpenAI()
    tools_by_name = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools] + [finish.schema()]
    reasoning = {"effort": effort, "summary": "auto"}
    result = AgentResult()
    transcript.parent.mkdir(parents=True, exist_ok=True)

    with transcript.open("w", encoding="utf-8") as log, tracer.start_as_current_span(
        name, attributes={KIND: OpenInferenceSpanKindValues.AGENT.value, INPUT: task}
    ) as span:
        def write(event: dict) -> None:
            log.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            log.flush()

        write({"type": "start", "agent": name, "model": model, "instructions": instructions,
               "task": task})
        next_input: str | list = task
        previous_id = None
        for turn in range(1, max_turns + 1):
            try:
                response = client.responses.create(
                    model=model, instructions=instructions, input=next_input, tools=schemas,
                    previous_response_id=previous_id, reasoning=reasoning,
                )
            except BadRequestError as exc:
                if "summary" not in str(exc) or "summary" not in reasoning:
                    raise
                reasoning = {"effort": effort}  # reasoning summaries need a verified org
                continue
            previous_id = response.id
            result.turns = turn
            _add_usage(result, response, model)

            calls = []
            for item in response.output:
                if item.type == "reasoning" and item.summary:
                    write({"turn": turn, "type": "thinking",
                           "text": " ".join(part.text for part in item.summary)})
                elif item.type == "message":
                    write({"turn": turn, "type": "message", "text": response.output_text})
                elif item.type == "function_call":
                    calls.append(item)
            if not calls:  # it answered in prose; remind it how to finish
                next_input = [{"role": "user", "content": f"Finish by calling {finish.name}."}]
                continue

            outputs = []
            for call in calls:
                result.tool_calls += 1
                args = json.loads(call.arguments or "{}")
                write({"turn": turn, "type": "tool_call", "tool": call.name, "args": args})
                if call.name == finish.name:
                    problems = validate(args)
                    if problems is None:
                        result.output, result.stop_reason = args, "finished"
                        write({"turn": turn, "type": "finished", "cost_usd": round(result.cost_usd, 4)})
                        span.set_attribute(OUTPUT, json.dumps(args)[:20000])
                        return result
                    output = f"REJECTED. Fix these problems and call {finish.name} again:\n{problems}"
                elif call.name in tools_by_name:
                    output = _run_tool(tools_by_name[call.name], args)
                else:
                    output = f"ERROR: there is no tool called {call.name}"
                write({"turn": turn, "type": "tool_result", "tool": call.name, "output": output})
                outputs.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": output})
            next_input = outputs

        write({"type": "stopped", "reason": "max_turns", "cost_usd": round(result.cost_usd, 4)})
        return result
