"""The RAG pipeline: rewrite the question, retrieve help-center chunks, generate an answer."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel

from support_bot import config
from support_bot.knowledge_base import Chunk, get_knowledge_base
from support_bot.tracing import CHAIN, INPUT, OUTPUT, RETRIEVER, SPAN_KIND, tracer

TOP_K = 10            # help-center chunks retrieved per question and put into the prompt
REWRITE_QUERY = True  # turn the customer's message into a search query with an extra LLM call

SYSTEM_PROMPT = """You are Orbi, the friendly support assistant for Orbitask, a project management app.

Customers hate hearing "I don't know", so always give a complete, confident answer.
Use the help-center excerpts you are given. If they do not fully cover the question,
fill the gaps with your general knowledge of how apps like Orbitask usually work.

Fill in:
- answer: your reply to the customer, friendly and concise
- confidence: "high", "medium" or "low"
- cited_chunk_ids: the ids of the excerpts you used, e.g. "billing-and-invoices#2"
- answered: true if you answered the question, false if you could not"""

REWRITE_PROMPT = (
    "Rewrite the customer's message as a short search query for a help center. "
    "Reply with the query only."
)


class ModelAnswer(BaseModel):
    """The structured output we ask the model for."""
    answer: str
    confidence: Literal["high", "medium", "low"]
    cited_chunk_ids: list[str]
    answered: bool


@dataclass
class BotReply:
    text: str
    confidence: str | None = None
    citations: list[str] = field(default_factory=list)
    answered: bool = False
    blocked: bool = False
    trace_id: str | None = None


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI()


def rewrite_query(question: str) -> str:
    with tracer.start_as_current_span(
        "rewrite_query", attributes={SPAN_KIND: CHAIN, INPUT: question}
    ) as span:
        response = _client().responses.create(
            model=config.BOT_MODEL, instructions=REWRITE_PROMPT, input=question, temperature=0,
        )
        query = response.output_text.strip()
        span.set_attribute(OUTPUT, query)
        return query


def retrieve(query: str, k: int = TOP_K) -> list[tuple[Chunk, float]]:
    with tracer.start_as_current_span(
        "retrieve", attributes={SPAN_KIND: RETRIEVER, INPUT: query, "retrieval.top_k": k}
    ) as span:
        hits = get_knowledge_base().search(query, k)
        for i, (chunk, score) in enumerate(hits):
            span.set_attribute(f"retrieval.documents.{i}.document.id", chunk.id)
            span.set_attribute(f"retrieval.documents.{i}.document.content", chunk.as_context())
            span.set_attribute(f"retrieval.documents.{i}.document.score", score)
        span.set_attribute("retrieval.top_score", hits[0][1] if hits else 0.0)
        return hits


def generate_answer(question: str, hits: list[tuple[Chunk, float]]) -> ModelAnswer:
    with tracer.start_as_current_span(
        "generate_answer", attributes={SPAN_KIND: CHAIN, INPUT: question}
    ) as span:
        excerpts = "\n\n".join(chunk.as_context() for chunk, _ in hits)
        response = _client().responses.parse(
            model=config.BOT_MODEL,
            instructions=SYSTEM_PROMPT,
            input=f"Help-center excerpts:\n\n{excerpts}\n\nCustomer message:\n{question}",
            text_format=ModelAnswer,
            temperature=0,
        )
        result = response.output_parsed
        span.set_attribute(OUTPUT, result.model_dump_json())
        return result


def answer(question: str) -> BotReply:
    query = rewrite_query(question) if REWRITE_QUERY else question
    hits = retrieve(query)
    result = generate_answer(question, hits)
    retrieved_ids = {chunk.id for chunk, _ in hits}
    return BotReply(
        text=result.answer,
        confidence=result.confidence,
        citations=[cid for cid in result.cited_chunk_ids if cid in retrieved_ids],
        answered=result.answered,
    )
