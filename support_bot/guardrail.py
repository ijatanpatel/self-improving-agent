"""Input guardrail: runs on a customer's message before any of it reaches the LLM.

1. Redacts payment card numbers, so they never reach the model or the traces.
2. Blocks obvious prompt-injection attempts.

It is plain rules on purpose: fast, free, predictable and easy to unit-test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from support_bot.tracing import GUARDRAIL, SPAN_KIND, tracer

CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

INJECTION_PATTERNS = [
    r"ignore (all |any )?(of )?(the |your )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any )?(of )?(the |your )?(previous|prior|above|earlier)",
    r"(reveal|show|print|repeat|output) (me )?(your|the) (hidden |system |secret )?(prompt|instructions)",
    r"you are now (in )?\w* ?mode",
    r"pretend (to be|you are) (an? )?(admin|administrator|developer|system)",
]


@dataclass
class GuardrailResult:
    passed: bool
    clean_text: str
    reason: str | None = None
    redactions: int = 0


def _passes_luhn(digits: str) -> bool:
    """The checksum every real card number satisfies; avoids redacting order numbers."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact_card_numbers(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _passes_luhn(digits):
            count += 1
            return "[REDACTED CARD]"
        return match.group()

    return CARD_NUMBER.sub(replace, text), count


def find_injection(text: str) -> str | None:
    lowered = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            return pattern
    return None


def check(text: str) -> GuardrailResult:
    with tracer.start_as_current_span("input_guardrail", attributes={SPAN_KIND: GUARDRAIL}) as span:
        clean_text, redactions = redact_card_numbers(text)
        pattern = find_injection(clean_text)
        result = GuardrailResult(
            passed=pattern is None,
            clean_text=clean_text,
            reason="prompt_injection" if pattern else None,
            redactions=redactions,
        )
        span.set_attribute("guardrail.passed", result.passed)
        span.set_attribute("guardrail.redactions", redactions)
        if result.reason:
            span.set_attribute("guardrail.reason", result.reason)
        return result
