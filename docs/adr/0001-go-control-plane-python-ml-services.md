# ADR-0001 — Go control plane, Python only for ML services

- **Status:** Accepted
- **Date:** 2026-08-27
- **Supersedes:** Project_Blueprint.md §4, which specifies FastAPI + Celery

## Context

The blueprint specifies a Python/FastAPI backend. The project owner is a backend engineer whose
working language is Go and who has no ML background. The ML libraries the project depends on
(`faster-whisper`, `pyannote.audio`, `sentence-transformers`, `scispaCy`) are Python-only with no
usable Go bindings.

## Decision

Go 1.23 owns the entire control plane: REST API, authentication, authorization, persistence,
orchestration, and the pipeline state machine. Python 3.11 is confined to two ML worker services
(`asr-service`, `nlp-service`) that do model inference and nothing else.

The boundary is enforced, not advisory:

- Go services contain no prompts, no model calls, no clinical field logic.
- Python services contain no workflow state, no retry policy, no authorization, no database writes
  outside their own stage-result rows and the LLM cache.

## Consequences

**Positive.** Business logic sits in the language the developer is fluent in, which is where the bugs
would otherwise be expensive. Python surface area stays small enough to be understood despite the
developer's inexperience with the domain. Python dependency hell (`scispaCy`'s spaCy pins, torch
builds) is quarantined in two containers and cannot destabilise the API.

**Negative.** A process boundary exists where a single-language system would have a function call,
which requires the transport machinery in ADR-0002. Contract types must be generated for two languages
(ADR-0004). Local development requires both toolchains.

**Neutral.** This diverges from the blueprint and from the prior DSCE project. The divergence is
deliberate and is recorded in the report as an engineering decision.

## Alternatives considered

- **Python throughout (blueprint's proposal).** Rejected: puts the orchestration and API logic in the
  developer's weaker language for no compensating benefit.
- **Go throughout, with ONNX-exported models.** Rejected: `pyannote` diarization and `scispaCy` have no
  practical Go path, and the export/conversion work would consume more time than the whole ML pipeline.
- **Go plus embedded Python (cgo).** Rejected: shared-process failure modes and GIL interactions are
  exactly the kind of hard-to-debug problem a solo developer cannot afford.
