"""Online evaluator: score recent production traces and attach the scores to them.

    uv run python -m evals.online_evaluator --hours 24
    uv run python -m evals.online_evaluator --watch 60    # keep scoring new traffic

For each request it reads the question, the answer and the retrieved help-center chunks
from Phoenix, runs DeepEval metrics with an LLM judge, and writes every score back to
Phoenix as an annotation on the request's root span. "Online" means it grades real
traffic after the fact, outside the request path, so it never slows the bot down.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from pathlib import Path
from typing import Callable

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
# Reasoning-model judges can be slow when many calls run at once; give each call room.
os.environ.setdefault("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE", "240")

import yaml  # noqa: E402
from deepeval.metrics import ContextualRelevancyMetric, FaithfulnessMetric, GEval  # noqa: E402
from deepeval.metrics.base_metric import BaseMetric  # noqa: E402
from deepeval.models import GPTModel  # noqa: E402
from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from loop.telemetry import RequestTrace, client, fetch_requests  # noqa: E402

load_dotenv()
# gpt-5-mini won a small calibration test: it caught 7 of 8 made-up answers, while
# gpt-4.1-mini also failed correct answers and gpt-4.1 let most made-up answers pass.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-5-mini")
PROJECT = os.getenv("PHOENIX_PROJECT", "support-bot")
CUSTOM_DIR = Path(__file__).resolve().parent / "custom"
# Re-scoring a span overwrites its previous score instead of adding a second one.
ANNOTATION_IDENTIFIER = "online-eval"


CONTEXT_SUFFICIENCY_STEPS = [
    "Identify exactly what the customer is asking for in the input.",
    "Check whether the retrieval context states the specific facts needed to answer it.",
    "Give a high score only if the full answer can be found in the retrieval context. "
    "Give a low score if the context is about other topics or only partly related.",
]


def metric_factories(judge: GPTModel) -> dict[str, Callable[[], BaseMetric]]:
    """Name -> function that builds a fresh metric (metrics keep state, so one per request)."""
    factories: dict[str, Callable[[], BaseMetric]] = {
        # Share of the answer's claims that the retrieved chunks support. By default DeepEval
        # only penalizes claims that contradict the context; we also penalize claims the
        # context does not mention, because those are hallucinations in a support bot.
        "faithfulness": lambda: FaithfulnessMetric(
            threshold=0.7, model=judge, penalize_ambiguous_claims=True),
        # Share of the retrieved text that is relevant to the question (retrieval noise).
        "contextual_relevancy": lambda: ContextualRelevancyMetric(threshold=0.5, model=judge),
        # Did retrieval find the answer at all? A small custom LLM-judge metric (G-Eval).
        "context_sufficiency": lambda: GEval(
            name="context_sufficiency",
            evaluation_steps=CONTEXT_SUFFICIENCY_STEPS,
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.RETRIEVAL_CONTEXT],
            threshold=0.5, model=judge, _include_g_eval_suffix=False,
        ),
    }
    # Evaluators that the loop drafted and a human approved (status: active).
    for path in sorted(CUSTOM_DIR.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if spec.get("status") != "active":
            continue
        params = [SingleTurnParams[p.upper()] for p in spec["evaluation_params"]]
        factories[spec["name"]] = lambda spec=spec, params=params: GEval(
            name=spec["name"], criteria=spec["criteria"], evaluation_params=params,
            threshold=spec.get("threshold", 0.5), model=judge,
        )
    return factories


def needs_scoring(request: RequestTrace, names: list[str], rescore: bool) -> bool:
    if request.root.attributes.get("answer.blocked") or not request.documents():
        return False  # blocked by the guardrail: there is no answer to grade
    return rescore or any(name not in request.evals for name in names)


async def score_request(request, factories, semaphore, rescore=False) -> tuple[list[dict], float]:
    test_case = LLMTestCase(
        input=request.question,
        actual_output=request.answer,
        retrieval_context=[str(doc["content"]) for doc in request.documents()],
    )
    annotations, cost = [], 0.0
    for name, make_metric in factories.items():
        if name in request.evals and not rescore:
            continue
        metric = make_metric()
        async with semaphore:
            try:
                await metric.a_measure(test_case, _show_indicator=False)
            except Exception as exc:  # a judge hiccup should not stop the whole batch
                print(f"  trace {request.trace_id[:8]} {name}: judge failed ({exc})")
                continue
        cost += metric.evaluation_cost or 0.0
        annotations.append({
            "name": name,
            "span_id": request.root.span_id,
            "annotator_kind": "LLM",
            "identifier": ANNOTATION_IDENTIFIER,
            "result": {
                "score": float(metric.score),
                "label": "pass" if metric.is_successful() else "fail",
                "explanation": metric.reason,
            },
            "metadata": {"judge_model": JUDGE_MODEL, "threshold": metric.threshold},
        })
    return annotations, cost


async def evaluate_once(project: str, hours: float, limit: int, concurrency: int,
                        rescore: bool = False) -> None:
    # Reasoning models only accept their default temperature; older models get 0 for
    # repeatable scores.
    judge = GPTModel(model=JUDGE_MODEL, **({"temperature": 0} if JUDGE_MODEL.startswith("gpt-4") else {}))
    factories = metric_factories(judge)
    requests = fetch_requests(project, hours)
    pending = [r for r in requests if needs_scoring(r, list(factories), rescore)][:limit]
    if not pending:
        print("Nothing new to score.")
        return

    print(f"Scoring {len(pending)} requests with {list(factories)} (judge: {JUDGE_MODEL})...",
          flush=True)
    semaphore = asyncio.Semaphore(concurrency)
    annotations, cost, done = [], 0.0, 0

    async def score_and_save(request: RequestTrace) -> None:
        # Save each request's scores as soon as they exist: progress is visible in Phoenix
        # right away, and a crash halfway loses nothing.
        nonlocal cost, done
        batch, batch_cost = await score_request(request, factories, semaphore, rescore)
        if batch:
            await asyncio.to_thread(client().spans.log_span_annotations,
                                    span_annotations=batch, sync=True)
        annotations.extend(batch)
        cost += batch_cost
        done += 1
        if done % 10 == 0 or done == len(pending):
            print(f"  scored {done}/{len(pending)}", flush=True)

    await asyncio.gather(*(score_and_save(r) for r in pending))
    print(f"Wrote {len(annotations)} annotations; judge cost about ${cost:.3f}")
    for name in factories:
        scores = [a["result"]["score"] for a in annotations if a["name"] == name]
        passed = sum(a["result"]["label"] == "pass" for a in annotations if a["name"] == name)
        if scores:
            print(f"  {name:22s} mean {statistics.mean(scores):.2f}   pass {passed}/{len(scores)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score production traces with DeepEval.")
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--hours", type=float, default=24, help="how far back to look")
    parser.add_argument("--limit", type=int, default=1000, help="max requests per pass")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--watch", type=int, default=0, help="re-run every N seconds")
    parser.add_argument("--rescore", action="store_true", help="re-score requests that already have scores")
    args = parser.parse_args()

    while True:
        asyncio.run(evaluate_once(args.project, args.hours, args.limit, args.concurrency,
                                  args.rescore))
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
