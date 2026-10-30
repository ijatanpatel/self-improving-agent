"""Entry points for the support channels. Every customer message becomes one trace."""
from __future__ import annotations

import re

from support_bot import config, guardrail, pipeline
from support_bot.pipeline import BotReply
from support_bot.tracing import CHAIN, INPUT, OUTPUT, SPAN_KIND, current_trace_id, tracer

CHANNELS = ("web", "email", "slack")

BLOCKED_REPLY = (
    "Sorry, I can't help with that request. For questions about your account, "
    "please contact support@orbitask.example."
)


def strip_email_noise(text: str) -> str:
    """Drop quoted replies ("> ...", "On ... wrote:") and the signature from an email body."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "--" or stripped.startswith("-- "):
            break
        if stripped.startswith(">") or re.match(r"^On .+ wrote:$", stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def handle_message(text: str, channel: str = "web") -> BotReply:
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r}; expected one of {CHANNELS}")

    with tracer.start_as_current_span(
        "support_request",
        attributes={SPAN_KIND: CHAIN, "app.channel": channel, "app.version": config.APP_VERSION},
    ) as span:
        if channel == "email":
            # Emails were already spam-filtered by our mail provider, so they go straight
            # to the pipeline once quoted replies and signatures are removed.
            question = strip_email_noise(text)
            reply = pipeline.answer(question)
        else:
            check = guardrail.check(text)
            question = check.clean_text
            if check.passed:
                reply = pipeline.answer(question)
            else:
                reply = BotReply(text=BLOCKED_REPLY, blocked=True)

        span.set_attribute(INPUT, question)
        span.set_attribute(OUTPUT, reply.text)
        span.set_attribute("answer.blocked", reply.blocked)
        span.set_attribute("answer.answered", reply.answered)
        span.set_attribute("answer.citation_count", len(reply.citations))
        if reply.confidence:
            span.set_attribute("answer.confidence", reply.confidence)
        reply.trace_id = current_trace_id(span)
        return reply
