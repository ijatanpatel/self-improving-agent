# Interview cheat sheet: Self-Improving Agent Loop

Plain-English notes for explaining this project. Read "The 30-second version" out loud a few
times; the rest is backup for follow-up questions.

## The 30-second version

> "I built a RAG support bot and an AI agent that keeps it healthy. Every request the bot
> handles is traced with OpenTelemetry into Arize Phoenix, and an online evaluator grades each
> answer with DeepEval LLM-judge metrics like faithfulness. A YAML health contract defines what
> healthy means: required steps, rules like 'high confidence needs good retrieval', and a
> latency SLO. When a rule breaks, an investigation agent I built on the OpenAI Responses API
> reads the traces and the code, finds the root cause, and files an evidence-backed issue. Then
> a fix agent opens a GitHub pull request in an isolated git worktree. Every cited trace and
> code line is verified automatically, and a human merges every PR."

## The 2-minute walkthrough (follow the arrows in the README diagram)

1. **The bot.** Customer asks a question (web, email or Slack). Guardrail → rewrite the
   question into a search query → retrieve help-center chunks with embeddings → GPT writes an
   answer with a confidence level and citations.
2. **Tracing.** Each of those steps is a *span*; one request is a *trace*. They go to Phoenix,
   so I can see exactly what happened in any request: inputs, outputs, timings, tokens.
3. **Online evals.** A separate process grades real answers after the fact with an LLM judge
   and attaches the scores to the trace. "Online" = grading live traffic, not a test set.
4. **Health contract.** A YAML file of rules. Plain Python checks every request against them.
   No LLM there, so the verdict is repeatable and free.
5. **Investigation agent.** Only when a rule fails. It uses small read-only "skills" (query
   traces, open a trace, map a span to code, read code) and must submit issues with real trace
   IDs and real code lines; my code verifies them and rejects anything made up.
6. **Issues + exits.** Issues go to SQLite, deduplicated by broken rule. For each one: save the
   failing requests as a regression dataset, draft a new evaluator, and have a fix agent open a
   PR. Then `verify` replays the regression set on main vs. the fix to measure before/after.

## Words you will be asked about

| Term | Say it like this |
|---|---|
| Trace / span | A trace is one request's full story; a span is one step in it (e.g. "retrieve"), with start time, duration and attributes. |
| OpenTelemetry | The industry-standard way to emit traces. Vendor-neutral, so I could swap Phoenix for Datadog or Langfuse without changing app code. |
| OpenInference | A set of attribute names on top of OpenTelemetry for LLM apps (e.g. `retrieval.documents`, `llm.token_count`), so tools understand LLM spans. |
| Arize Phoenix | Open-source observability UI for LLM apps. Runs locally; stores traces, eval scores (annotations) and datasets. |
| Annotation | A label or score attached to a span after the fact. My evaluator writes its scores as annotations. |
| RAG | Retrieval-augmented generation: find relevant documents first, then let the LLM answer using them. |
| LLM-as-a-judge | Using a (different, stronger) LLM to grade another LLM's output against criteria. |
| Faithfulness | Share of the answer's claims that are supported by the retrieved text. Low = hallucination. |
| Contextual relevancy | Share of the retrieved text that is actually relevant to the question. Low = noisy retrieval. |
| Context sufficiency (my G-Eval metric) | Did retrieval find the answer at all? G-Eval = DeepEval's way to define a judge metric from written criteria/steps. |
| Health contract / invariant | A rule that must always hold, e.g. "if confidence is high, retrieval must contain the answer". |
| Error budget | How many violations a rule may have before it counts as failing (like SRE error budgets). Needed because judges have false alarms. |
| p95 latency / SLO | 95% of requests are faster than this. The SLO (service-level objective) is the target: p95 < 5 s. |
| Tool calling / agent loop | The model asks to call a function; my code runs it and sends back the result; repeat until it calls "finish". |
| Skills | My name for the agent's small, read-only tools, each answering one question with a short summary so the context stays small. |
| Git worktree | A second checkout of the same repo in another folder, so the fix agent works on its own branch without touching mine. |
| Regression dataset | The failing requests saved so they can be replayed on any version to prove a fix works and catch it if it breaks again. |

## Design decisions and the "why" (most likely follow-ups)

**Why doesn't the agent decide what is broken?**
Counting violations is a job for code: deterministic, cheap, testable. I only use the LLM for the
judgment parts (root cause, which code, what fix). That also means the agent can't invent a
problem: it only explains rules that the checker already proved are failing.

**How do you stop the agent from hallucinating evidence?**
The finish tool's input is validated in code. Every trace ID must exist in the snapshot and must
have broken that rule; every file must exist and every line number must be in range. If anything
fails, the errors go back to the agent and it has to fix its submission. Tested in
`tests/test_findings_validation.py`.

**How do you stop the fix agent from "cheating"?**
It has no shell and no git, just file tools locked to an allowlist (`support_bot/`, `tests/`).
It can't touch the health contract, the evaluator, the loop, the help-center facts or `.env`. So
it can't make a rule pass by weakening the measurement. The loop runs the tests itself instead of
trusting the agent's claim, and a human merges every PR.

**How do you know the LLM judge is right?** (best story, it actually happened)
I didn't assume it. First, DeepEval's faithfulness by default only penalizes claims that
*contradict* the context, so a made-up "Orbitask has no Jira integration" scored 1.0. I turned on
`penalize_ambiguous_claims`. Then the cheap judge (`gpt-4.1-mini`) started failing correct answers,
e.g. a correct SOC 2 answer got 0.0. I compared three judges on the same answers: `gpt-4.1` let 7
of 8 made-up answers pass, `gpt-5-mini` caught 7 of 8 and had half the false alarms, so I picked
it and added error budgets for the remaining noise. Next step would be a hand-labeled set of ~50
answers to measure agreement properly.

**Why did you add your own metric when DeepEval has contextual relevancy?**
Contextual relevancy measures the share of retrieved text that is relevant. With 10 chunks, even
a perfect retrieval scored 0.04, because 9 chunks were noise. That's a useful noise signal (it
supports cutting top-k), but it can't answer "did we find the answer?". So I added a G-Eval metric,
context sufficiency, for the "high confidence needs good retrieval" rule.

**Why deduplicate by rule instead of title?**
The agent words titles differently every run. The broken rule is stable, so the same problem
updates one issue (times seen, last seen) instead of creating duplicates. Issues resolve when the
rule passes and reopen as "regressed" if it breaks again.

**Why one PR per file at a time?**
Two of the problems both pointed at `pipeline.py`. Opening two PRs on the same file means merge
conflicts, and the first fix often changes the second problem. So the loop opens the first, waits
for the merge and a fresh measurement, then deals with what is left. That's how a careful team
would do it too.

**Why OpenTelemetry instead of just logging?**
Traces keep the parent/child structure (which step happened inside which request) and timings,
and they're a standard, so any backend can read them and my agent can query them.

**What does it cost?** Fill in from `python -m loop` output: judge about $0.03 per request with
`gpt-5-mini`; each investigation and fix run prints its own cost.

**What would you do next?** Hand-labeled judge calibration; run the evaluator continuously and
the investigation on a schedule; split issues by root cause, not only by rule; cost and
token-usage rules in the contract; a small dashboard for issue history.

## What I would say I learned

- An eval is only as good as its judge: test the judge before trusting its numbers.
- Metrics have assumptions (contextual relevancy punishes large top-k); read what they measure.
- Agents need guardrails in code, not just in the prompt: allowlists, verification, human review.
- Observability first: once every step was traced, finding causes was a query, not a guess.

## Numbers to remember

(Filled in after the end-to-end run; see the Results section of the README.)
