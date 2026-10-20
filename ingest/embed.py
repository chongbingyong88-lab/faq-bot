"""Embeddings via the Gemini API.

Previously BGE-m3 plus a bge-reranker cross-encoder, both local. That cost
~45s per reply on a 4-core laptop — 43s of it in the reranker doing 20
forward passes — and 4GB of RAM. Neither survives on a cheap VPS, and a
social-media bot cannot make someone wait 45 seconds.

Now: one network call, no torch, ~100MB of process. gemini-embedding-001
is multilingual, so EN/MS/ZH still share a vector space.

The reranker is gone. Vector search alone put the right chunk in the top 20
on 97% of eval rows, so it was reordering rather than rescuing.
"""
from __future__ import annotations

import math
import os

EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
# pgvector's HNSW index refuses more than 2000 dimensions and Gemini
# defaults to 3072, so ask for fewer. Reduced-dimension embeddings arrive
# unnormalised (Google's docs say to normalise them yourself).
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
BATCH = int(os.getenv("EMBED_BATCH", "64"))

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai

        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("set GEMINI_API_KEY in .env")
        _client = genai.Client(api_key=key)
    return _client


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def _embed(texts: list[str], task: str) -> list[list[float]]:
    from google.genai import types

    from .llm import is_retryable

    client = _get_client()
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i : i + BATCH]
        for attempt in range(6):
            try:
                resp = client.models.embed_content(
                    model=EMBED_MODEL,
                    contents=chunk,
                    config=types.EmbedContentConfig(
                        task_type=task, output_dimensionality=EMBED_DIM),
                )
                out.extend(_normalise(e.values) for e in resp.embeddings)
                break
            except Exception as exc:
                if not is_retryable(exc) or attempt == 5:
                    raise
                import random
                import time
                time.sleep(2 ** (attempt + 1) + random.uniform(0, 1.5))
    return out


def embed_texts(texts: list[str], batch_size: int | None = None) -> list[list[float]]:
    """Embed stored chunks. Documents and queries use different task types."""
    return _embed(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> list[float]:
    return _embed([text], "RETRIEVAL_QUERY")[0]
