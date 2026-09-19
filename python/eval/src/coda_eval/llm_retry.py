"""Wraps an `LLMClient` so every individual call retries on
`QuotaExhaustedError`, rather than only the outermost eval-runner call
(`coda_eval.retry.complete_with_quota_retry`) doing so.

Found live 2026-09-05 running `coda-eval run-got-eval` for the first time:
`qwen/qwen3.8-27b`'s real free-tier limit is an 8000 **token**-per-minute
window (claude_context.md §4 already documented this figure), not a request
count — confirmed via the raw `x-ratelimit-*` response headers, which showed
`limit-tokens: 8000` while `limit-requests` had 997 of 1000 untouched. A
single generation call's prompt (graph context for all 8 fields) costs a
sizeable fraction of that window on its own, so two calls back-to-back
routinely 429s the second one. `run_reasoning` alone makes on the order of a
dozen such calls per consultation (N candidate-generation calls plus up to
N x K refinement calls per field), and `got_eval.py` wraps the *entire*
`run_reasoning` invocation in one `complete_with_quota_retry` — so a 429 on,
say, the ninth internal call re-tries the whole pipeline from call one
(harmless token-wise, since `llm_cache` makes every already-succeeded call a
free cache hit — but it burns the outer retry's small, bounded attempt
budget on internal calls that individually just needed to wait out a TPM
window). Retrying at the client level instead means each 429 is resolved
exactly where it happened, so the outer retry budget is never spent on
problems the client already solved.

This wrapper is `coda_eval`-only. The live pipeline's `GroqLLMClient` is
deliberately left alone: architecture.md §2.5 wants `QuotaExhaustedError` to
reach the orchestrator so it can **park** the job (`resume_after`, no
attempt consumed) rather than block a worker retrying in place — a policy
this offline, no-orchestrator harness has no equivalent of and no reason to
imitate.
"""

from __future__ import annotations

from coda_eval.retry import complete_with_quota_retry
from nlp_service.llm.client import LLMClient, LLMCompletion


class RetryingLLMClient:
    """Same `LLMClient` shape as the one it wraps; every `complete()` call
    retries on `QuotaExhaustedError` before the exception can reach a caller
    that only expects to retry a much larger unit of work."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

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
    ) -> LLMCompletion:
        return await complete_with_quota_retry(
            lambda: self._inner.complete(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                timeout_s=timeout_s,
                json_mode=json_mode,
                max_tokens=max_tokens,
            ),
            description=f"{model} completion",
        )


__all__ = ["RetryingLLMClient"]
