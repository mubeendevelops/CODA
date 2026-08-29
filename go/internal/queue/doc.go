// Package queue is the Redis Streams transport between go-orchestrator and
// the Python ML workers (ADR-0002, docs/architecture.md §2).
//
// It owns five things and deliberately nothing else:
//
//  1. Publishing StageEnvelope to stage.asr / stage.nlp, guarded by
//     producer-side idempotency keys (producer.go).
//  2. Consuming a consumer group via XREADGROUP, acknowledging only after
//     the handler reports durable persistence (consumer.go, §2.3).
//  3. Reclaiming entries a dead consumer left pending, via XAUTOCLAIM past
//     a per-stage visibility timeout (reclaim.go, §2.4).
//  4. Routing over-attempt and FATAL messages to stage.dlq with the full
//     per-attempt failure history (dlq.go, §2.5).
//  5. The retry policy itself: exponential backoff with full jitter
//     (backoff.go) and the per-stage max-attempt / timeout table
//     (policy.go, §4.2).
//
// What it does *not* own is the decision to retry. The worker classifies a
// failure (OK / RETRYABLE / FATAL / QUOTA_EXHAUSTED / CANCELLED); the
// orchestrator in package pipeline decides what that means for the job.
// This package only carries messages and supplies the numbers.
//
// Correctness rests on the database, not on this package. Delivery is
// at-least-once, duplicates are expected and routine, and the guarantee
// that a duplicate does no work is job_stages.idempotency_key UNIQUE
// (ADR-0007) — not any check here. The producer-side dedupe guard in
// PublishEnvelope is an optimisation that reduces duplicate *deliveries*;
// losing it degrades to the behaviour the DB constraint already handles.
//
// go-api uses exactly one thing from this package: the Enqueuer interface,
// satisfied in production by StreamEnqueuer, which rings a doorbell and
// dispatches nothing (§1.2 forbids go-api from dispatching stages or
// consuming streams).
package queue
