"""LLMClient: the provider-swappable abstraction nlp-service's extraction
and summary calls go through. `GroqLLMClient` (groq.py) is the only real
backend today (claude_context.md §4/§5 decision: Groq free tier); the
Protocol exists so a test can substitute `CassetteLLMClient` (cassette.py)
without any caller-side branching, and so a future second provider is a new
class, not a rewrite of extraction.py/summary.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """One completed chat call, with the accounting a caller needs to
    aggregate into StageMetrics (tokens, latency, cost) without reaching
    into provider-specific response shapes.
    """

    content: str
    tokens_in: int
    tokens_out: int
    model: str
    latency_ms: int
    cost_estimate: float


class LLMClient(Protocol):
    """A single JSON- or text-mode chat completion. Implementations classify
    failures per coda_worker_sdk.errors' taxonomy (RetryableError,
    FatalError, QuotaExhaustedError) so the exception boundary routes them
    correctly — see groq.py for the real classification logic.

    `max_tokens` defaults to the provider's own default (`None` passes
    nothing through) — added for callers whose response can be large enough
    that a reasoning model's hidden reasoning tokens crowd out its final
    content, discovered empirically by `coda_eval.metrics.hallucination`'s
    judge call: `openai/gpt-oss-20b` returned an empty completion (Groq's
    `json_validate_failed` with an empty `failed_generation`) against a
    larger claim set with no `max_tokens` set. Existing callers (extraction/
    summary) are unaffected by leaving it unset.
    """

    async def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMCompletion: ...


__all__ = ["LLMClient", "LLMCompletion"]
