"""Fake LLMClient for tests: replays a scripted sequence of completions (or
typed failures) instead of ever calling a network. This is what keeps CI off
a paid API — every nlp-service test that exercises extraction/summary logic
should construct a CassetteLLMClient, never mock `httpx` or hit Groq.

A cassette is a JSON file: a list of "turns", each either
`{"content": "...", "tokens_in": N, "tokens_out": N}` (a normal completion)
or `{"error": "fatal" | "retryable" | "quota_exhausted", "message": "..."}`
(a typed failure). `complete()` consumes turns in order, one per call,
regardless of the prompt content passed in — this is a sequence-of-calls
script (e.g. "invalid JSON, then a valid repair"), not a request/response
cache keyed by prompt hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from coda_worker_sdk.errors import FatalError, QuotaExhaustedError, RetryableError
from nlp_service.llm.client import LLMCompletion


@dataclass(slots=True)
class CassetteTurn:
    content: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None  # "fatal" | "retryable" | "quota_exhausted"
    message: str = ""


@dataclass(slots=True)
class CassetteLLMClient:
    """Constructed directly with `turns=[...]`, or via `from_file(path)` to
    load a JSON fixture from `tests/fixtures/cassettes/`.
    """

    turns: list[CassetteTurn] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    """Every call's kwargs, in order — assert against this to check the
    caller actually issued the calls a test expects (e.g. that a repair
    retry's user_prompt contains the validation error)."""

    @classmethod
    def from_file(cls, path: Path) -> CassetteLLMClient:
        data = json.loads(path.read_text(encoding="utf-8"))
        turns = [CassetteTurn(**turn) for turn in data]
        return cls(turns=turns)

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
        self.calls.append(
            {
                "model": model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
            }
        )
        if not self.turns:
            raise AssertionError("CassetteLLMClient: no turns left to replay")
        turn = self.turns.pop(0)

        if turn.error == "fatal":
            raise FatalError(
                turn.message or "cassette: scripted fatal error", code="CASSETTE_FATAL"
            )
        if turn.error == "retryable":
            raise RetryableError(
                turn.message or "cassette: scripted retryable error", code="CASSETTE_RETRYABLE"
            )
        if turn.error == "quota_exhausted":
            raise QuotaExhaustedError(turn.message or "cassette: scripted quota exhaustion")

        return LLMCompletion(
            content=turn.content or "",
            tokens_in=turn.tokens_in,
            tokens_out=turn.tokens_out,
            model=model,
            latency_ms=0,
            cost_estimate=0.0,
        )


__all__ = ["CassetteLLMClient", "CassetteTurn"]
