"""The help-center knowledge base: split articles into chunks, embed them, search them.

The corpus is ~100 chunks, so the vectors live in a NumPy array cached on disk.
A vector database only becomes worth it at a much larger scale.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from openai import OpenAI

from support_bot import config


@dataclass(frozen=True)
class Chunk:
    id: str      # e.g. "billing-and-invoices#2"
    title: str   # e.g. "Billing and invoices > When you are charged"
    text: str

    def as_context(self) -> str:
        return f"[{self.id}] {self.title}\n{self.text}"


def load_chunks(kb_dir: Path = config.KB_DIR) -> list[Chunk]:
    """One chunk per '## ' section, so each chunk covers a single topic."""
    chunks = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        article = text.splitlines()[0].removeprefix("# ").strip()
        for n, section in enumerate(text.split("\n## ")[1:], start=1):
            heading, _, body = section.partition("\n")
            chunks.append(Chunk(f"{path.stem}#{n}", f"{article} > {heading.strip()}", body.strip()))
    return chunks


def _embed(texts: list[str]) -> np.ndarray:
    response = OpenAI().embeddings.create(model=config.EMBED_MODEL, input=texts)
    vectors = np.array([item.embedding for item in response.data], dtype=np.float32)
    # Unit-length vectors make cosine similarity a plain dot product.
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray):
        self.chunks = chunks
        self.vectors = vectors

    @classmethod
    def build(cls) -> "KnowledgeBase":
        """Embed every chunk, reusing the on-disk cache when the articles have not changed."""
        chunks = load_chunks()
        fingerprint = hashlib.sha256(
            json.dumps([config.EMBED_MODEL] + [c.as_context() for c in chunks]).encode()
        ).hexdigest()[:16]
        cache = config.INDEX_DIR / f"{fingerprint}.npy"
        if cache.exists():
            vectors = np.load(cache)
        else:
            vectors = _embed([c.as_context() for c in chunks])
            config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
            np.save(cache, vectors)
        return cls(chunks, vectors)

    def search(self, query: str, k: int) -> list[tuple[Chunk, float]]:
        """Return the k chunks most similar to the query, best first, with cosine scores."""
        scores = self.vectors @ _embed([query])[0]
        best = np.argsort(-scores)[:k]
        return [(self.chunks[i], float(scores[i])) for i in best]


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    return KnowledgeBase.build()
