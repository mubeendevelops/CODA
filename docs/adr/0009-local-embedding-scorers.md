# ADR-0009 — Local embedding and lexical scorers for two of three GoT criteria

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0008, ADR-0011

## Context

GoT-HCS scores candidate thoughts on relevance, consistency, and redundancy (Eq. 19–22) using trained
scorer heads. Training them requires labeled graph data this project does not have. The obvious
substitute — LLM-as-judge for all three criteria — would triple the scoring token cost, and scoring
runs over N=3 candidates × 8 fields × K=2 iterations per consultation, making it the largest single
consumer of a capped budget.

Project_Blueprint.md §7 anticipates this: "a simple heuristic: entity overlap with source = relevance,
embedding similarity to thought = consistency, n-gram overlap with prior selections = redundancy."

## Decision

Implement the *intent* of Eq. 19–22 with a mixed scorer:

| Criterion | Implementation | Cost |
|---|---|---|
| **Relevance** | LLM-as-judge rubric on `openai/gpt-oss-20b` + entity overlap with source turns | API |
| **Consistency** | Local `all-MiniLM-L6-v2` cosine similarity between candidate and its supporting thoughts | **free** |
| **Redundancy** | n-gram overlap against previously selected content | **free** |

Weights are configurable via `RunConfig.scorer_weights` and persisted with every result, making the
weighting itself an ablatable dimension.

## Consequences

**Positive.** Scoring token cost drops by roughly two-thirds against an all-LLM judge. Local scorers
are deterministic and reproducible — no temperature, no provider drift, so a re-run reproduces
identical scores. No training data required. Faithful to the base paper's three-criterion structure,
which keeps the ablation legible to anyone who has read it.

**Negative.** Embedding similarity is a weaker consistency proxy than a trained head: it detects
semantic divergence but not clinical contradiction (a candidate asserting the opposite of a thought may
still score as similar). n-gram redundancy misses paraphrase. Both are documented limitations, and the
report must state that two of three criteria are heuristic rather than learned.

**Mitigation.** Relevance — the criterion most directly tied to hallucination, the headline metric —
retains an LLM judge. The cheapest criteria were the ones downgraded.

## Alternatives considered

- **LLM-as-judge for all three.** Rejected on token budget (ADR-0008); would roughly triple the
  dominant cost for a marginal quality gain on two criteria.
- **Train the scorer heads.** Rejected: no labeled data, no GPU, and out of scope per decision #7.
- **A clinical embedding model (BioLORD, S-PubMedBERT).** Deferred, not rejected. `embed_model` is a
  `RunConfig` field, so this is a config change and a legitimate future ablation.
