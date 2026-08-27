# ADR-0006 — Centralized orchestrator-owned state machine, not event choreography

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0002, ADR-0007

## Context

The pipeline has ordered stages with a mandatory redaction step, retries, quota parking, a human
review gate, and cancellation. Two structures are possible: a central orchestrator that owns state and
dispatches each stage, or choreography in which each worker emits an event that triggers the next.

## Decision

`go-orchestrator` exclusively owns the pipeline state machine (`docs/architecture.md` §4). Workers are
stateless functions: they receive a stage request, do work, return a result. They do not know what
runs next, do not decide retries, and do not transition state.

## Consequences

**Positive.** "Where is consultation X and why is it stuck" is one `SELECT` against `jobs` and
`job_stages` — the single most valuable property for a solo developer debugging a five-service
pipeline. Retry, backoff, quota-park, and timeout policy live in one place in one language rather than
being duplicated across two Python workers. The mandatory redaction step (ADR-0014) is enforceable
precisely because one component decides transitions; under choreography, "NLP must never run on
unredacted text" would be a convention that any worker bug could break. Cancellation has one authority.

**Negative.** The orchestrator is a single point of failure and a coordination bottleneck — acceptable
because it is stateless between messages and restart-safe (state lives in Postgres). Adding a stage
means editing the orchestrator, so services are less independently deployable. This is the classic
orchestration/choreography tradeoff, resolved toward debuggability.

## Alternatives considered

- **Event choreography.** Rejected: better decoupling, worse observability. Reconstructing pipeline
  state means correlating events across services, and no single component could enforce the redaction
  invariant.
- **A workflow engine (Temporal, Cadence).** Rejected: genuinely the right tool for durable execution
  with retries and timeouts, and would supply much of §4 off the shelf. Rejected on operational weight
  — another server plus its own datastore in a laptop Compose file — and on the learning curve for a
  solo developer whose scarce resource is time, not orchestration correctness.
