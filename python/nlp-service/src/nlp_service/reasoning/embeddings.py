"""Embedding backends for the consistency scorer (Module 5).

claude_context.md §4 pins `all-MiniLM-L6-v2` via sentence-transformers, and
§6's keep/simplify/replace table promises "local MiniLM embedding similarity
to supporting thoughts" as the zero-API substitute for the paper's trained
consistency head. That commitment is kept here — but behind an interface with
a dependency-free fallback, for a reason worth stating: sentence-transformers
pulls in torch, the model is a ~90MB first-run download, and neither is
available in CI, an offline sandbox, or a fresh clone. A GoT arm that cannot
run at all without a network is worse than one that degrades.

What must never happen is a lexical score silently claiming to be an
embedding similarity. Every `ScoreBreakdown` therefore carries
`consistency_backend` (`minilm` | `lexical`) recording what actually ran, and
the eval can group by it. A number whose provenance is unknown is worth less
than a weaker number whose provenance is recorded (decision #97).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Protocol

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class EmbeddingBackend(Protocol):
    """`similarity` returns cosine-like similarity in [0, 1] between one
    candidate string and each of several reference strings."""

    name: str

    def similarity(self, text: str, references: list[str]) -> list[float]: ...


class LexicalEmbeddingBackend:
    """TF-IDF-weighted token cosine over the reference set.

    Not a learned embedding and never claimed to be one — it cannot see that
    "shortness of breath" and "dyspnoea" are the same thing, which is exactly
    the gap MiniLM exists to close. It is a defined, deterministic,
    dependency-free floor so the pipeline runs everywhere, and it is honest
    about being that: `name = "lexical"` reaches the persisted score.

    IDF is computed over the reference set itself (the supporting thoughts of
    one field), which is small. That is the point: within one field's
    subgraph, a term appearing in every supporting thought carries little
    discriminative signal, and one appearing in a single thought carries a
    lot.
    """

    name = "lexical"

    def similarity(self, text: str, references: list[str]) -> list[float]:
        if not references:
            return []
        ref_tokens = [tokenize(r) for r in references]
        n_docs = len(ref_tokens)
        doc_freq: Counter[str] = Counter()
        for toks in ref_tokens:
            doc_freq.update(set(toks))

        def idf(term: str) -> float:
            # Smoothed: a term in every reference still gets a small positive
            # weight rather than exactly zero, so a candidate that echoes the
            # subgraph's common vocabulary is not scored as unrelated to it.
            return math.log((n_docs + 1) / (doc_freq.get(term, 0) + 1)) + 1.0

        def vec(tokens: list[str]) -> dict[str, float]:
            counts = Counter(tokens)
            if not counts:
                return {}
            max_c = max(counts.values())
            return {t: (0.5 + 0.5 * c / max_c) * idf(t) for t, c in counts.items()}

        def cosine(a: dict[str, float], b: dict[str, float]) -> float:
            if not a or not b:
                return 0.0
            dot = sum(v * b.get(k, 0.0) for k, v in a.items())
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            if na == 0.0 or nb == 0.0:
                return 0.0
            return max(0.0, min(1.0, dot / (na * nb)))

        cand = vec(tokenize(text))
        return [cosine(cand, vec(toks)) for toks in ref_tokens]


class MiniLMEmbeddingBackend:
    """sentence-transformers `all-MiniLM-L6-v2` (claude_context.md §4).

    Constructed lazily and only on demand — importing sentence-transformers
    costs several seconds and imports torch, which no other nlp-service code
    path needs. `load()` returns None rather than raising when the package or
    the model weights are unavailable, which is what lets `resolve_backend`
    fall back instead of failing the run.
    """

    name = "minilm"

    def __init__(self, model: object) -> None:
        self._model = model

    @classmethod
    def load(cls, model_name: str) -> MiniLMEmbeddingBackend | None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.info(
                "sentence-transformers not installed; consistency scoring will "
                "use the lexical fallback",
                extra={"extra_fields": {"embed_model": model_name}},
            )
            return None
        try:
            return cls(SentenceTransformer(model_name))
        except Exception as exc:
            # A missing local cache with no network, a corrupt download, an
            # unrecognized model name. Degrading is correct; a scorer is not
            # worth failing a whole consultation over.
            logger.warning(
                "could not load the embedding model; falling back to lexical consistency",
                extra={"extra_fields": {"embed_model": model_name, "error": str(exc)}},
            )
            return None

    def similarity(self, text: str, references: list[str]) -> list[float]:
        if not references:
            return []
        import numpy as np

        vectors = self._model.encode(  # type: ignore[attr-defined]
            [text, *references], normalize_embeddings=True
        )
        cand = np.asarray(vectors[0])
        # Cosine of unit vectors is the dot product; clamped because floating
        # point can put a self-similarity a hair above 1.0, and a score
        # outside [0, 1] would break the aggregate's documented range.
        return [float(np.clip(float(np.dot(cand, np.asarray(v))), 0.0, 1.0)) for v in vectors[1:]]


_DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
_cache: dict[str, EmbeddingBackend] = {}


def resolve_backend(embed_model: str = "") -> EmbeddingBackend:
    """Returns MiniLM when it loads, the lexical fallback otherwise. Cached
    per model name — loading the transformer for every field of every
    consultation would dominate the stage's wall-clock.
    """
    name = embed_model or _DEFAULT_EMBED_MODEL
    cached = _cache.get(name)
    if cached is not None:
        return cached
    backend: EmbeddingBackend = MiniLMEmbeddingBackend.load(name) or LexicalEmbeddingBackend()
    _cache[name] = backend
    return backend


def reset_cache() -> None:
    """Tests pin a specific backend; without this they would inherit
    whichever one the first test in the process happened to resolve."""
    _cache.clear()


__all__ = [
    "EmbeddingBackend",
    "LexicalEmbeddingBackend",
    "MiniLMEmbeddingBackend",
    "resolve_backend",
    "reset_cache",
    "tokenize",
]
