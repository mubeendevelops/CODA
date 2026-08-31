# ADR-0008 — Token budget as an architectural constraint; multi-bucket model assignment

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0009, ADR-0015, decision #6 (budget ₹0)
- **Amended 2026-08-30 (claude_context.md decision #71):** `llama-3.3-70b-versatile` (generation
  bucket) and `llama-3.1-8b-instant` (structural bucket) were both removed from Groq's served model
  list entirely between this ADR's date and decision #71. Replaced with `qwen/qwen3.8-27b` and
  `qwen/qwen3.6-27b` respectively — the bucket-splitting *design* below is unchanged, only the two
  model names are stale wherever they appear in this document.

## Context

The project budget is zero. Groq's free tier caps **tokens per day**, per model:

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `llama-3.3-70b-versatile` | 30 | 1K | 12K | **100K** |
| `openai/gpt-oss-120b` | 30 | 1K | 8K | **200K** |
| `openai/gpt-oss-20b` | 30 | 1K | 8K | **200K** |
| `whisper-large-v3-turbo` | 20 | 2K | — | 28.8K audio-sec/day |

One GoT run over one consultation costs roughly 33K tokens. A 6-arm × 30-consultation ablation is
several million tokens — on a single model's daily cap, that is over a month of wall-clock. Treated as
an operational detail, this would silently make the project's central deliverable unachievable.

## Decision

Treat the token budget as a first-class architectural constraint. Five mechanisms:

1. **Multi-bucket model assignment.** Roles are pinned to different models so their daily quotas *sum*
   rather than compete: generation on `llama-3.3-70b-versatile`, judging on `openai/gpt-oss-20b`,
   structural calls (edge typing, role assignment) on `llama-3.1-8b-instant`, reference-label
   generation on `openai/gpt-oss-120b`. Combined ≈ 500K+ TPD.
2. **Local scorers.** Two of the three GoT criteria — consistency (MiniLM embedding similarity) and
   redundancy (n-gram overlap) — run locally at zero API cost (ADR-0009).
3. **Graph-structured context retrieval.** Only the relevant thought neighbourhood enters each prompt,
   not the full transcript. This is simultaneously the architecture's core mechanism (ADR-0011) and its
   largest token saving.
4. **Field batching.** One call covering all 8 fields per candidate pass, not 8 calls.
5. **Response caching** keyed on `(model, sha256(prompt))` (ADR-0015).

Quota exhaustion is modelled as a **normal condition**: `QUOTA_EXHAUSTED` parks the job with
`resume_after` and does **not** increment the attempt counter (`docs/architecture.md` §2.5).

## Consequences

**Positive.** The ablation becomes achievable in days rather than weeks. Quota exhaustion resumes
automatically instead of dead-lettering the entire eval run — without the parking rule, hitting a daily
cap would burn all three attempts within seconds and fail every queued job. Per-model token accounting
in `job_stages.metrics` yields the cost-per-consultation figure the report needs anyway.

**Negative.** Different models across roles means the judge is weaker than the generator, which is
acceptable for rubric scoring but must be disclosed. Stage timeouts are sized against TPM throttling
rather than compute (a 33K-token consultation cannot finish faster than ~3–4 minutes of pure throttle).
Provider-side limit changes would change project wall-clock (assumption A4).

**Critical invariant.** The model under test is held **constant** across `baseline` and every `got_*`
arm. Varying it would confound the experiment. An assertion at eval start enforces this.

## Alternatives considered

- **Single model for all roles.** Rejected: forfeits the summed quota and makes the ablation
  impractically slow.
- **Paid tier.** Out of scope — decision #6.
- **Fully local inference (Ollama).** Rejected as primary: laptop-CPU 7B inference is far slower than
  throttled hosted 70B, and quality would not support the headline claim. Retained for dev-loop
  debugging so plumbing work never burns quota.
