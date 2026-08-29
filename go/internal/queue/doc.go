// Package queue owns the interface go-api enqueues pipeline jobs against.
// The real implementation — Redis Streams XADD to stage.asr with
// consumer-group delivery, XAUTOCLAIM stalled-message recovery, and
// stage.dlq routing (docs/architecture.md §2) — is go-orchestrator's job,
// not go-api's; go-orchestrator does not exist yet (plan.md Phase 3).
//
// Until it does, Enqueuer is implemented by NoopEnqueuer: POST
// /consultations/{id}/jobs creates the jobs row (the durable record of
// "this needs processing") and calls Enqueue, which today only logs.
// Swapping in a real Redis-backed Enqueuer later is a one-line change at
// the call site in cmd/api/main.go — no handler code moves.
package queue
