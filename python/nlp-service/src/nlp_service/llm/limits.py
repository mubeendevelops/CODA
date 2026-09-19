"""Shared `max_tokens` ceiling for Groq JSON-mode calls on the reasoning
models (`qwen/qwen3.8-27b`, `qwen/qwen3.6-27b`).

Two distinct failure modes were found live 2026-09-05, in this order:

1. **Hidden reasoning starving the answer.** With no `max_tokens` set (or
   `reasoning_format` left at Groq's default), a Qwen3 hybrid think/no-think
   model can spend nearly its entire completion on a hidden `<think>` trace
   before ever emitting the requested JSON — surfacing as
   `json_validate_failed` with an EMPTY `failed_generation`. Fixed at the
   client level (`nlp_service.llm.groq.GroqLLMClient.complete`) with
   `reasoning_format: "hidden"` + `reasoning_effort: "none"` for every Qwen3
   json_mode call. Once reasoning is off, real completions are small — an
   isolated one-key-JSON test dropped from 454 completion tokens to 12.

2. **Output-Tokens-Per-Minute (OTPM).** Groq enforces a *separate* per-minute
   ceiling on `max_tokens` itself, invisible in the standard
   `x-ratelimit-limit-tokens` header and only surfaced as a distinct error:
   `"Request too large ... on output tokens per minute (OTPM): Limit 1000,
   Requested 2000"`. This session's OTPM allowance was observed to *drop* to
   1000 partway through a long run (multiple large-`max_tokens` requests had
   succeeded earlier in the same session at 4000-6000) — consistent with
   Groq applying temporary, usage-adaptive throttling rather than a fixed
   per-key ceiling. A `max_tokens` above whatever the current OTPM ceiling
   is gets rejected outright, on every attempt, with no amount of waiting
   fixing it — unlike the transient per-minute-window 429s
   `coda_eval.retry` already handles by waiting.

Point 1 already removed the reason to request a large ceiling (no more
hidden-reasoning risk to buffer against), so this constant intentionally
stays well under the lowest OTPM value observed live, trading a small,
recoverable risk of truncation (the existing schema-validation repair loop
retries a truncated/invalid response) against a large, unrecoverable risk of
outright rejection.

This ceiling alone was not sufficient for `nlp_service.reasoning.generation`,
which originally batched multiple fields into one call: real
`source_thought_ids` are full database UUIDs (not the short "t1"-style ids
test fixtures use), and one verbose field with several citations was
observed live to consume this entire budget by itself, truncating a
multi-field response before the other fields were ever written. Fixed there
by making one call cover exactly one field
(`generation.FIELDS_PER_GENERATION_CALL`) rather than by raising this
ceiling — the ceiling is the fixed point; call granularity is what had to
give.

3. **Declared `max_tokens`, not actual usage, is what OTPM charges against.**
   Confirmed live: a 429's "Requested" figure exactly echoed the declared
   `max_tokens` regardless of how much a call would actually generate. One
   consequence, also found live: even after splitting to one field per call,
   a field retrieving many supporting thoughts (one cited 19 of them) needed
   ~750 real output tokens — most of it citation UUIDs — meaning declaring
   `OTPM_SAFE_MAX_TOKENS` (900) for every such call reserves nearly the
   *entire* 1000-token window per call, capping real throughput at roughly
   one field per minute regardless of how small most fields' actual answers
   are. Fixed two ways together: a citation cap (at most 6
   `source_thought_ids` per field, enforced by both the prompt and
   `reasoning.schema.CANDIDATE_FIELD_SCHEMA`'s `maxItems`) bounds realistic
   worst-case output, and `SINGLE_FIELD_MAX_TOKENS` below declares a smaller
   ceiling for calls that cover one field (generation, refinement) so more
   of them fit in each OTPM window — `OTPM_SAFE_MAX_TOKENS` stays reserved
   for the whole-transcript/whole-graph calls (thought construction, edge
   prediction) that genuinely need the extra room.
"""

from __future__ import annotations

OTPM_SAFE_MAX_TOKENS = 900

SINGLE_FIELD_MAX_TOKENS = 500
"""For calls whose response covers exactly one field (generation, critique/
refinement) — see point 3 above. A citation-capped field's realistic worst
case is ~350-400 tokens (a long value plus 6 full-UUID citations); this
leaves real margin while roughly doubling how many such calls fit in one
OTPM window versus declaring the full `OTPM_SAFE_MAX_TOKENS`."""

__all__ = ["OTPM_SAFE_MAX_TOKENS", "SINGLE_FIELD_MAX_TOKENS"]
