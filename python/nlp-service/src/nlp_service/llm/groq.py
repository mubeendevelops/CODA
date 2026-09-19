"""Real LLMClient backend: Groq chat completions. Same retry/backoff/error-
classification shape as asr_service/groq_client.py (full-jitter exponential
backoff, 429 -> QuotaExhaustedError, 4xx -> FatalError, 5xx/timeout exhausted
-> RetryableError) but generalized over model/temperature/timeout per call,
since nlp-service's extraction and summary calls use different prompts
against the same base_model, not one fixed call shape.

cost_estimate is always 0.0: Groq's free tier has no per-token billing
(claude_context.md decision #6 — $0 budget; §4's model table is all
free-tier). A paid provider implementing LLMClient later would compute a
real figure here; nothing downstream assumes 0.0 specifically, so that swap
needs no caller-side change.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from coda_worker_sdk.errors import FatalError, QuotaExhaustedError, RetryableError
from nlp_service.llm.client import LLMCompletion

logger = logging.getLogger(__name__)

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 8.0


class GroqLLMClient:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key

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
        payload: dict[str, object] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            # Reasoning models wrap their answer in a hidden <think> trace by
            # default. "hidden" returns only the final answer, which is all
            # any json_mode caller in this codebase ever reads.
            payload["reasoning_format"] = "hidden"
            # Found live 2026-09-05 (coda-eval run-got-eval's first real run):
            # a qwen/qwen3.6-27b edge-prediction call 400'd with
            # code=json_validate_failed and an EMPTY failed_generation —
            # raising max_tokens up to 4000+ made no difference. Root cause,
            # isolated with a minimal curl request: Qwen3's hybrid think/
            # no-think mode defaults to thinking even for a one-key JSON
            # reply (445 of 454 completion tokens were hidden reasoning in
            # that isolated test), and for this system's actual structured-
            # extraction prompts the reasoning trace can apparently exceed
            # any bounded max_tokens entirely, leaving zero tokens for the
            # answer. `reasoning_effort: "none"` (Qwen3's documented
            # non-thinking mode) confirmed live to fix this exactly — the
            # same isolated request dropped from 454 completion tokens to 12
            # with it set. None of this system's json_mode calls are
            # open-ended reasoning tasks (they are all schema-constrained
            # extraction/classification), so there is no quality reason to
            # keep Qwen3's thinking mode on, and every reason to: it was
            # actively breaking the request.
            #
            # Scoped to Qwen3 models only — gpt-oss (the judge/reference-
            # label model) is a different reasoning implementation without a
            # documented "none" effort level, and its existing scaled-
            # max_tokens fix (coda_eval.metrics.hallucination, decision #75)
            # was already validated live without needing this.
            if model.startswith("qwen/"):
                payload["reasoning_effort"] = "none"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        started = time.monotonic()
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(
                        GROQ_CHAT_COMPLETIONS_URL, json=payload, headers=headers
                    )
            except httpx.TimeoutException as exc:
                last_exc = exc
                await _backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                await _backoff(attempt)
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                raise QuotaExhaustedError(
                    f"groq quota exhausted for model {model}",
                    provider_status="429",
                    retry_after_seconds=float(retry_after) if retry_after else None,
                )
            if resp.status_code >= 500:
                last_exc = RuntimeError(f"groq {resp.status_code}: {resp.text[:200]}")
                await _backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise FatalError(
                    f"groq request rejected for model {model}: "
                    f"{resp.status_code} {resp.text[:300]}",
                    code="GROQ_BAD_REQUEST",
                    provider_status=str(resp.status_code),
                )

            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            latency_ms = int((time.monotonic() - started) * 1000)
            return LLMCompletion(
                content=content,
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
                model=model,
                latency_ms=latency_ms,
                cost_estimate=0.0,
            )

        raise RetryableError(
            f"groq unreachable for model {model} after {_MAX_ATTEMPTS} attempts: {last_exc}",
            code="GROQ_UNREACHABLE",
        )


async def _backoff(attempt: int) -> None:
    delay = min(_BASE_BACKOFF_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
    await asyncio.sleep(delay * (0.5 + random.random() / 2))


__all__ = ["GroqLLMClient"]
