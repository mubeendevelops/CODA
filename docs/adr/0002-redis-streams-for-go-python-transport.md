# ADR-0002 — Redis Streams with consumer groups as the Go↔Python transport

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0001, ADR-0003, ADR-0005, ADR-0007

## Context

ADR-0001 creates a process boundary between the Go orchestrator and two Python ML workers. Pipeline
stages are long-running (ASR up to 20 minutes on CPU; the GoT stage up to 45 minutes under free-tier
token throttling), failure-prone (network, provider 5xx, quota exhaustion), and must survive worker
crashes without recomputation or data loss.

Redis is already in the stack for Asynq (Go-internal jobs) and rate-limit counters.

## Decision

Stage dispatch and results flow over **Redis Streams with consumer groups**. The orchestrator `XADD`s
to `stage.asr` / `stage.nlp`; workers consume via `XREADGROUP` on per-service groups and `XADD`
results to `stage.results`. Stalled entries are reclaimed with `XAUTOCLAIM`. Poison messages route to
`stage.dlq`. Delivery is at-least-once with idempotency-key deduplication (ADR-0007).

## Consequences

**Positive.** Consumer groups give per-message acknowledgement, a pending-entries list, and
`XAUTOCLAIM`-based recovery — the three primitives actually needed for crash-safe long work — without
adding infrastructure. Streams are durable and replayable, so a poison message can be inspected and
re-driven. Protojson payloads (ADR-0004) mean stream contents are readable with `XRANGE` during
debugging, which matters disproportionately for a solo developer.

**Negative.** Redis becomes load-bearing for pipeline correctness, so its persistence configuration
(AOF) is now a correctness concern rather than a cache setting. Consumer-group semantics must be
understood properly; naive `XACK` placement would silently lose work (mitigated by the
acknowledge-after-persist rule in `docs/architecture.md` §2.3).

## Alternatives considered

- **gRPC only (synchronous calls for everything).** Rejected: a 45-minute unary call has no sane
  deadline, no crash recovery, and no backpressure. A dead worker loses the job outright. gRPC is
  retained for genuinely fast synchronous calls — ADR-0003.
- **Kafka.** Rejected: correct choice at high throughput and multi-consumer fan-out, neither of which
  applies here. Cost is a JVM broker, ZooKeeper/KRaft operations, and a much heavier Compose file on a
  laptop, for ordering and retention guarantees this workload does not need.
- **Asynq for everything.** Rejected: Asynq is Go-native with no Python client. Using it across the
  language boundary would mean reimplementing its Redis key layout in Python — a private protocol
  reimplemented by hand, which is strictly worse than using Streams as a documented public one. Asynq
  is kept for Go-internal jobs (exports, erasure, reaping) where both ends are Go.
- **RabbitMQ.** Rejected: mature and capable, but adds a broker to operate for capabilities Streams
  already provide at this scale.
- **Postgres as a queue (`SELECT … FOR UPDATE SKIP LOCKED`).** Rejected as primary: viable and
  attractively simple, but couples worker polling to the primary datastore and offers no equivalent to
  `XAUTOCLAIM` without hand-rolled lease logic.
