"""Consultation summary generation: a separate free-text LLM call (not
JSON-schema-gated). Basic sanity validation only — non-empty, above a
configurable minimum length — since there is no repair loop for free text
(claude_context.md §3: `summary` is scored separately from the 8 fields).
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.llm.client import LLMClient
from nlp_service.llm_cache import complete_cached


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int


async def run_summary(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    transcript_turns_text: str,
    min_chars: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> SummaryResult:
    pr = prompts.load_summary_prompts(language=language)
    user_prompt = pr.user_template.format(transcript_turns=transcript_turns_text)

    completion, cache_hit = await complete_cached(
        conn,
        llm_client,
        model=model,
        system_prompt=pr.system,
        user_prompt=user_prompt,
        temperature=0.2,
        timeout_s=timeout_s,
        json_mode=False,
    )

    text = completion.content.strip()
    if len(text) < min_chars:
        raise FatalError(
            f"summary response too short ({len(text)} chars, min {min_chars}): {text!r}",
            code="SUMMARY_TOO_SHORT",
        )

    return SummaryResult(
        text=text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
        llm_calls=1,
        cache_hits=1 if cache_hit else 0,
    )


__all__ = ["SummaryResult", "run_summary"]
