# ADR-0015 — Postgres-backed LLM response cache keyed on (model, prompt hash)

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0007, ADR-0008, ADR-0012

## Context

The ablation runs the same consultations through six arms. Several arms share early stages: thought
construction under identical config produces an identical prompt whether the run later uses K=1 or K=2.
Development involves repeatedly re-running the pipeline while debugging downstream stages. Against a
100–200K token daily cap, re-paying for identical calls is the difference between an eval sweep that
finishes in days and one that does not finish.

`plan.md` Phase 5 has as an acceptance criterion that killing the eval runner and restarting resumes
without re-spending tokens. That is not achievable without a cache.

## Decision

A Postgres table `llm_cache` with `UNIQUE (model, prompt_sha256)`, storing the response, token counts,
and a hit counter. Every provider call goes through a wrapper that checks the cache first.

Postgres rather than Redis because the cache must be durable across restarts and across days, is
queryable for analysis ("how many tokens did the GoT arm actually spend net of cache"), and is included
in the database dump that makes results reproducible (ADR-0012).

The key deliberately excludes `run_config_id`: two configs producing an identical prompt to an identical
model *should* share the response. Config differences that matter reach the cache key through the prompt
text itself.

## Consequences

**Positive.** Shared stages across arms are paid for once. Re-running an eval after a downstream bug fix
is nearly free. Development iteration stops consuming the research budget. `hit_count` and stored token
counts give an exact measure of gross versus net token cost, which feeds the cost-per-consultation
analysis the report needs. Combined with stage-level idempotency (ADR-0007), there are two independent
layers protecting quota.

**Negative.** Cached responses make the pipeline non-deterministic in an unexpected direction: a cached
call cannot exhibit the sampling variance a fresh call would, so a re-run is *more* reproducible than
the original experiment was. This is desirable for reproducibility and misleading for any variance
estimate — variance studies must bypass the cache explicitly. Temperature > 0 means the cached response
is one sample, not the distribution. A cache entry that is subtly wrong is now wrong permanently, so
invalidation must be possible (prompt edits change `prompt_sha256`, giving natural invalidation;
`prompt_set_hash` in `RunConfig` records which prompt version produced a result).

Cache growth is unbounded but negligible at this volume.

## Alternatives considered

- **Redis cache.** Rejected: eviction under memory pressure would silently lose entries the token
  budget depends on, and it would not be part of the reproducibility dump.
- **On-disk file cache.** Rejected: not queryable, and not included in a database dump.
- **No cache.** Rejected: makes the Phase 5 resumability criterion unachievable and the eval sweep
  impractical.
