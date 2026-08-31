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
