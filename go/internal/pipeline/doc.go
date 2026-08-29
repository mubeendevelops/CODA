// Package pipeline is go-orchestrator's state machine (ADR-0006,
// docs/architecture.md §4). It owns the jobs and job_stages tables
// exclusively: dispatch, retry and backoff, quota parking, timeout
// enforcement, stalled-message recovery, DLQ routing, and cancellation.
//
// # What it must not contain
//
// No prompts, no clinical logic, no field definitions, no LLM calls, and no
// decision that depends on clinical content (§1.2). It routes opaque
// payload references. Anything in this package that had to know what a
// clinical field is would be an architecture bug.
//
// # Crash recovery
//
// There is no recovery procedure, because a job's row *is* its resume
// point. Every write ordering here is chosen so that a process killed
// between any two steps leaves the job in a state some loop already scans
// for:
//
//   - killed mid-dispatch  -> job still in a rest state -> DispatchOnce
//   - killed mid-result    -> message unacknowledged    -> reclaim sweep
//   - killed while running -> stage row stalls          -> ReapOnce
//
// This is why dispatch publishes *before* marking a job running, and why
// the acknowledgement of a result comes strictly after its transaction
// commits (§2.3).
//
// # Duplicates
//
// Delivery is at-least-once and duplicates are routine, not exceptional.
// ApplyResult is therefore written as a convergent operation — "make the
// database consistent with this result" — rather than as a
// deduplication check, so applying the same result twice reaches the same
// state as applying it once, including finishing an advance that a crash
// interrupted halfway. The hard guarantee underneath is
// job_stages.idempotency_key UNIQUE (ADR-0007), not any check in this
// package.
//
// # Quota
//
// QUOTA_EXHAUSTED is not a failure and does not consume an attempt (§2.5).
// On a token-per-day free tier this is the difference between an eval sweep
// that pauses overnight and one that dead-letters every job in it.
package pipeline
