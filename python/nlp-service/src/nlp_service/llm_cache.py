"""Wires the Postgres `llm_cache` table as an actual cache (architecture.md
§5.5, decision #28) in front of `LLMClient.complete()` — a cache hit costs no
tokens and no network call, which is decision #28's whole point (dev
iteration and eval re-runs shouldn't burn the free-tier quota).

Cache key is `(model, sha256(system_prompt + user_prompt))` — the repair
loop's retry calls carry a different `user_prompt` (the repair addendum is
appended), so a repair attempt is never wrongly cache-hit against the
original failed attempt.
"""

from __future__ import annotations

import psycopg

from nlp_service import db
from nlp_service.llm.client import LLMClient, LLMCompletion


async def complete_cached(
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    timeout_s: float = 60.0,
    json_mode: bool = False,
) -> tuple[LLMCompletion, bool]:
    """Returns (completion, cache_hit)."""
    sha = db.prompt_sha256(system_prompt, user_prompt)
    cached = await db.get_cached_completion(conn, model=model, sha256=sha)
    if cached is not None:
        return cached, True

    completion = await llm_client.complete(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        timeout_s=timeout_s,
        json_mode=json_mode,
    )
    await db.store_completion(conn, model=model, sha256=sha, completion=completion)
    return completion, False


__all__ = ["complete_cached"]
