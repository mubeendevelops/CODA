# ADR-0016 — Build a working vertical slice before the full infrastructure

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0017, decision #21

## Context

The target topology is substantial: two Go services, two Python workers, Redis Streams with consumer
groups, protobuf contracts, Postgres with migrations, MinIO, auth, and a React frontend. Built in
dependency order, nothing AI-shaped would be demonstrable for several sessions.

The project owner needs demonstrable proof of initialization quickly (decision #16), has no fixed
deadline, and is a backend engineer with no ML background — meaning the ML path carries all the
technical uncertainty while the infrastructure path carries almost none.

## Decision

Phase 1 builds a deliberately minimal vertical slice — direct HTTP, local filesystem, no queue, no
auth — around **real** models: Groq Whisper, real pyannote diarization, a real single-pass extraction
producing a schema-valid 8-field note. Phase 3 then replaces the plumbing with the full topology.

The ML code written in Phase 1 survives into Phase 3. Only the transport is discarded.

## Consequences

**Positive.** An audio-to-note result exists within roughly two sessions. The riskiest assumptions —
A1 (Whisper on Kannada-accented English) and A2 (pyannote wall-clock on laptop CPU) — are tested before
any infrastructure is built to depend on them; if A1 fails badly, that is discovered while the cost of
changing course is near zero. The developer builds the unfamiliar ML path while the familiar Go path is
still simple. Phase 3 then refactors *working* code with a known-good end-to-end result as its
regression test, rather than integrating two untested halves.

**Negative.** Phase 1's transport code is written to be thrown away, which is real duplicated effort and
feels wasteful. There is a risk the throwaway slice ossifies into permanent architecture if Phase 3 is
deferred — mitigated by Phase 3 being a required phase with its own acceptance criteria, and by Phase 1
having no auth or persistence, so it cannot serve as a plausible product.

## Alternatives considered

- **Infrastructure first, ML workers stubbed.** Rejected: defers all technical risk to the end of the
  project. The stubs would validate the plumbing while leaving every genuinely uncertain question — does
  ASR work on the target language, does diarization finish in acceptable time — untested until the
  schedule is committed.
- **Full topology built in dependency order in one pass.** Rejected: longest time to first demonstrable
  result, and highest cost if an early ML assumption proves wrong.
