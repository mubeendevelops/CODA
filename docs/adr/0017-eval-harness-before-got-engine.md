# ADR-0017 — Build the evaluation harness before the GoT engine

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0012, ADR-0013, ADR-0015, ADR-0016
- **Supersedes:** Project_Blueprint.md §10, which schedules the evaluation suite as Phase 7 of 8

## Context

The blueprint places the full evaluation suite near the end of the roadmap, while simultaneously
instructing (§10, Phase 3) to "ablate against your Phase 2 baseline immediately as you build, not at the
end." These are contradictory: ablating as you build requires a harness that does not yet exist.

The GoT engine is the project's core contribution and its largest phase (17% weight). It has many
tunable dimensions — N, K, scorer weights, graph neighbourhood size, prompt design — and no prior
intuition about which settings work on ASR-derived conversational transcripts.

## Decision

The evaluation harness is Phase 5; the GoT engine is Phase 6. The harness must be able to produce a
full metrics table for the frozen baseline arm before the GoT engine is written.

## Consequences

**Positive.** Every GoT design choice is measurable at the moment it is made, so tuning is empirical
rather than guessed. A regression introduced while adding refinement iterations is caught immediately
rather than at the end, when attributing it among weeks of changes would be far harder. The baseline arm
is measured and frozen before the experimental arm exists (ADR-0012's constant-`base_model` invariant is
verifiable from the start). The token-accounting and resumability machinery the harness needs
(ADR-0015) exists before the most expensive phase begins, so the GoT development loop itself is cheap.

**Negative.** The headline result arrives later in the project than it would if the GoT engine were
built first, which can feel like slow progress. The harness is built against only one arm, so its
multi-arm handling is unexercised until Phase 6 — a design flaw there surfaces one phase late.

**Non-negotiable.** With a single developer and no deadline, the failure mode this ordering prevents —
building the entire contribution, measuring once at the end, and discovering it does not work with no
time or budget left to diagnose why — is the one that would end the project. Rebuilding an engine is
recoverable; discovering unmeasured work was wrong is not.

## Alternatives considered

- **Blueprint ordering (eval last).** Rejected: contradicts the blueprint's own ablate-as-you-build
  instruction, and makes the largest phase unmeasurable while it is being built.
- **Minimal ad-hoc metrics during Phase 6, full harness later.** Rejected: ad-hoc metrics computed
  inconsistently across a phase are not comparable to each other, so the intermediate numbers guiding
  development would be untrustworthy — the harness would end up rebuilt anyway.
