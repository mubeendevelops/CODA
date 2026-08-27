# ADR-0007 — At-least-once delivery with database-enforced idempotency

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0002, ADR-0006, ADR-0015

## Context

Redis Streams provide at-least-once delivery. Duplicate deliveries occur on worker crash between work
completion and `XACK`, and on `XAUTOCLAIM` reclaiming an entry whose worker was merely slow. Stages are
expensive: a duplicate GoT stage costs ~33K tokens against a 100K/day quota, so duplicate execution is
not merely wasteful but can exhaust the day's budget.

## Decision

Accept at-least-once delivery and make every stage idempotent.

```
idempotency_key = sha256(consultation_id ‖ stage ‖ run_config_id ‖ input_artifact_sha256)
```

The key includes `run_config_id` and the input hash deliberately: the same consultation under a
different ablation arm is different work; a redelivery for the same arm is the same work.

Enforcement is at the database layer — `job_stages.idempotency_key` is `UNIQUE`. Before doing work a
worker checks for a succeeded row with that key and, if present, re-emits the stored `result_ref`
without recomputing. `XACK` occurs **only after** the result is durably persisted and the artifact
committed.

## Consequences

**Positive.** Effectively-once outcomes over at-least-once delivery, with no distributed transaction.
The guarantee is a database constraint rather than application discipline, so it holds even if worker
code is wrong. Crash-mid-stage costs at most one redundant no-op delivery. Quota is protected from
duplicate expensive work, which is the outcome that actually matters here.

**Negative.** Every worker must implement the check-claim-work-persist-ack sequence correctly, and
getting `XACK` placement wrong silently loses work — the failure mode is invisible rather than loud.
A crash after persistence but before `XACK` produces one wasted redelivery cycle. Content-hashing
inputs adds a read before every stage.

## Alternatives considered

- **Exactly-once delivery.** Rejected: not available from Redis Streams, and not achievable in general
  across a process boundary without distributed transactions. Idempotent consumers are the standard
  answer.
- **At-most-once (ack on receipt).** Rejected: a worker crash would silently drop a consultation.
- **In-memory dedupe cache in the worker.** Rejected: does not survive the restart that causes the
  duplicate in the first place.
