"""Minimal async Groq chat-completions client for the role classifier —
scoped to what asr-service needs (one JSON-mode call), not a general SDK.
Classifies failures per architecture.md §2.5's taxonomy so the exception
boundary in coda_worker_sdk can route them correctly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx

from coda_worker_sdk.errors import FatalError, QuotaExhaustedError, RetryableError

logger = logging.getLogger(__name__)

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0


class GroqCompletionResult:
    def __init__(self, content: str, tokens_in: int, tokens_out: int) -> None:
        self.content = content
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


async def chat_json(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_s: float = 30.0,
) -> GroqCompletionResult:
    """One JSON-mode chat completion, with retry/backoff on transient
    failures and explicit classification of 429 as quota exhaustion
    (architecture.md §2.5 — parked, not counted against the retry budget).
    """
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(GROQ_CHAT_COMPLETIONS_URL, json=payload, headers=headers)
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
                "groq role-classifier quota exhausted",
                provider_status="429",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"groq {resp.status_code}: {resp.text[:200]}")
            await _backoff(attempt)
            continue
        if resp.status_code >= 400:
            raise FatalError(
                f"groq role-classifier request rejected: {resp.status_code} {resp.text[:300]}",
                code="GROQ_BAD_REQUEST",
                provider_status=str(resp.status_code),
            )

        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        return GroqCompletionResult(
            content=content,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
        )

    raise RetryableError(
        f"groq role-classifier unreachable after {_MAX_ATTEMPTS} attempts: {last_exc}",
        code="GROQ_UNREACHABLE",
    )


async def _backoff(attempt: int) -> None:
    delay = min(_BASE_BACKOFF_S * (2 ** (attempt - 1)), 8.0)
    await asyncio.sleep(delay * (0.5 + random.random() / 2))


def parse_json_object(raw: str) -> dict[str, object]:
    try:
        result: dict[str, object] = json.loads(raw)
        return result
    except json.JSONDecodeError as exc:
        raise FatalError(
            f"groq role-classifier returned non-JSON content: {exc}", code="GROQ_BAD_JSON"
        ) from exc


__all__ = ["GroqCompletionResult", "chat_json", "parse_json_object"]
