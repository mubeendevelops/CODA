//go:build integration

package pipeline

import (
	"context"
	"testing"
	"time"

	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// TestIntegration_FatalResultRoutesToDLQImmediately is plan.md Phase 3
// acceptance criterion 2: "a deliberately poisoned message lands in
// stage.dlq with its failure history intact".
//
// FATAL bypasses the retry budget entirely (§2.5) — malformed input or an
// unsupported schema will fail identically next time, so spending two more
// attempts on it buys nothing and, for the NLP stage, would burn ~66K
// tokens of a 100K daily budget proving it.
func TestIntegration_FatalResultRoutesToDLQImmediately(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-dlq-fatal", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)
	env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)

	poison := &codev1.Error{
		Code:           "SCHEMA_UNSUPPORTED",
		Message:        "envelope declares schema_version 9, this worker implements 1",
		Retryable:      false,
		ProviderStatus: "",
	}
	h.publishResult(t, env, codev1.Status_STATUS_FATAL, "", poison, nil)

	final := h.waitForJobState(t, job.ID, 15*time.Second, StateDeadLettered)
	if final.State != string(StateDeadLettered) {
		t.Fatalf("a FATAL result must dead-letter the job, got %q", final.State)
	}

	// ASR's MaxAttempts is 3, but FATAL must not have consumed the other
	// two — no retry was published.
	row := h.stage(t, job.ID, "asr")
	if row.Attempt != 1 {
		t.Errorf("FATAL should not consume retries: asr attempt = %d, want 1", row.Attempt)
	}
	if row.Status != "failed" {
		t.Errorf("asr status = %q, want failed", row.Status)
	}
	if n := h.rdb.XLen(h.ctx, queue.StreamASR).Val(); n != 1 {
		t.Errorf("a FATAL result must not trigger a re-dispatch; stage.asr holds %d envelopes", n)
	}

	// The DLQ message itself — §2.5: "carries the original envelope, every
	// attempt's error, the full trace, and the terminal classification.
	// Nothing is silently dropped."
	dls := h.deadLetters(t)
	if len(dls) != 1 {
		t.Fatalf("expected exactly 1 dead letter, got %d", len(dls))
	}
	dl := dls[0]

	if dl.GetJobId() != job.ID.String() {
		t.Errorf("dead letter job_id = %q, want %q", dl.GetJobId(), job.ID)
	}
	if dl.GetConsultationId() != job.ConsultationID.String() {
		t.Errorf("dead letter consultation_id = %q, want %q", dl.GetConsultationId(), job.ConsultationID)
	}
	if dl.GetStage() != codev1.Stage_STAGE_ASR {
		t.Errorf("dead letter stage = %s, want STAGE_ASR", dl.GetStage())
	}
	if dl.GetFinalStatus() != codev1.Status_STATUS_FATAL {
		t.Errorf("dead letter final_status = %s, want STATUS_FATAL", dl.GetFinalStatus())
	}
	if dl.GetDeadLetteredAt() == nil {
		t.Error("dead letter has no dead_lettered_at timestamp")
	}

	// The original envelope must be complete enough to replay from —
	// §2.5's "replay is an explicit operator action that re-XADDs with
	// attempt = 1" needs every field an operator cannot reconstruct.
	orig := dl.GetOriginalEnvelope()
	if orig == nil {
		t.Fatal("dead letter carries no original envelope — it cannot be replayed")
	}
	if orig.GetIdempotencyKey() != env.GetIdempotencyKey() {
		t.Errorf("replay envelope idempotency_key = %q, want %q", orig.GetIdempotencyKey(), env.GetIdempotencyKey())
	}
	if orig.GetRunConfigId() != job.RunConfigID.String() {
		t.Errorf("replay envelope run_config_id = %q, want %q", orig.GetRunConfigId(), job.RunConfigID)
	}
	if orig.GetPayloadRef() != env.GetPayloadRef() {
		t.Errorf("replay envelope payload_ref = %q, want %q", orig.GetPayloadRef(), env.GetPayloadRef())
	}
	if orig.GetTraceId() != job.TraceID {
		t.Errorf("replay envelope trace_id = %q, want %q — the audit trail must join to the trace", orig.GetTraceId(), job.TraceID)
	}
	if orig.GetLabels()["arm"] != "baseline" {
		t.Errorf("replay envelope lost its arm label: %v", orig.GetLabels())
	}

	// Failure history: one entry, carrying the worker's own classification.
	attempts := dl.GetAttempts()
	if len(attempts) != 1 {
		t.Fatalf("expected 1 attempt in the failure history, got %d", len(attempts))
	}
	if attempts[0].GetError().GetCode() != "SCHEMA_UNSUPPORTED" {
		t.Errorf("failure history lost the error code: %q", attempts[0].GetError().GetCode())
	}
	if attempts[0].GetError().GetMessage() != poison.GetMessage() {
		t.Errorf("failure history lost the error message: %q", attempts[0].GetError().GetMessage())
	}
	if attempts[0].GetOccurredAt() == nil {
		t.Error("failure history entry has no occurred_at")
	}

	// And the same history is in Postgres, so the DLQ message stays
	// reconstructible after Redis trims the stream.
	if got := errorHistory(t, row); len(got) != 1 {
		t.Errorf("job_stages.error_history should mirror the DLQ attempts, got %v", got)
	}
}

// TestIntegration_RetryableExhaustsAttemptsThenDeadLetters is the other DLQ
// route: RETRYABLE failures that use up the stage's §2.5 budget. ASR's
// budget is 3.
func TestIntegration_RetryableExhaustsAttemptsThenDeadLetters(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-dlq-retry", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	const maxAttempts = 3 // §4.2's asr row
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 15*time.Second)
		if got := int(env.GetAttempt()); got != attempt {
			t.Fatalf("expected attempt %d, envelope says %d", attempt, got)
		}
		h.publishResult(t, env, codev1.Status_STATUS_RETRYABLE, "", &codev1.Error{
			Code: "PROVIDER_UNAVAILABLE", Message: "groq returned 503", Retryable: true, ProviderStatus: "503",
		}, nil)
		if attempt < maxAttempts {
			// Between attempts the job parks in its rest state with a
			// backoff, then the dispatch scan picks it up again.
			h.waitForJobState(t, job.ID, 15*time.Second, StateASRQueued, StateASRRunning)
		}
	}

	final := h.waitForJobState(t, job.ID, 20*time.Second, StateDeadLettered)
	if final.State != string(StateDeadLettered) {
		t.Fatalf("job should be dead_lettered after %d retryable failures, is %q", maxAttempts, final.State)
	}

	row := h.stage(t, job.ID, "asr")
	if row.Attempt != maxAttempts {
		t.Errorf("asr attempt = %d, want %d", row.Attempt, maxAttempts)
	}
	// Every attempt is recorded, not just the last — this is the whole
	// point of error_history over a single `error` column.
	if got := errorHistory(t, row); len(got) != maxAttempts {
		t.Errorf("error_history should hold %d entries, got %d: %v", maxAttempts, len(got), got)
	}

	dls := h.deadLetters(t)
	if len(dls) != 1 {
		t.Fatalf("expected 1 dead letter, got %d", len(dls))
	}
	if n := len(dls[0].GetAttempts()); n != maxAttempts {
		t.Errorf("dead letter should carry all %d attempts, carries %d", maxAttempts, n)
	}
	if dls[0].GetFinalStatus() != codev1.Status_STATUS_RETRYABLE {
		t.Errorf("final_status = %s, want STATUS_RETRYABLE (the classification that ran out of attempts)", dls[0].GetFinalStatus())
	}
}

// TestIntegration_QuotaExhaustionParksWithoutConsumingAnAttempt covers §2.5's
// "quota parking is not a retry" — described there as the single most
// important operational detail of the system.
//
// On a token-per-day free tier a daily cap is routine. If it counted as a
// retry, one cap would burn all three attempts within seconds and
// dead-letter every job in the eval sweep.
func TestIntegration_QuotaExhaustionParksWithoutConsumingAnAttempt(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-quota", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)
	env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	if env.GetAttempt() != 1 {
		t.Fatalf("first dispatch should be attempt 1, got %d", env.GetAttempt())
	}
	// Wait for the running transition before publishing: dispatch publishes
	// the envelope *before* marking the job running (so a crash in between
	// is recoverable), which means readEnvelope can return while the job is
	// still in asr_queued — the same state a quota park lands in.
	h.waitForJobState(t, job.ID, 10*time.Second, StateASRRunning)

	// The provider says "come back in an hour".
	resumeAfter := time.Now().Add(time.Hour)
	h.publishResult(t, env, codev1.Status_STATUS_QUOTA_EXHAUSTED, "", &codev1.Error{
		Code: "QUOTA_EXHAUSTED", Message: "daily token cap reached", ProviderStatus: "429",
	}, &resumeAfter)

	parked := h.waitForJobState(t, job.ID, 15*time.Second, StateASRQueued)
	if parked.State != string(StateASRQueued) {
		t.Fatalf("a quota-exhausted job must park in its *_QUEUED state, is %q", parked.State)
	}
	if !parked.ResumeAfter.Valid {
		t.Fatal("a parked job must carry resume_after, or nothing will ever wake it")
	}
	if !parked.ResumeAfter.Time.After(time.Now().Add(30 * time.Minute)) {
		t.Errorf("resume_after = %s, want roughly the provider's one-hour retry-after", parked.ResumeAfter.Time)
	}

	row := h.stage(t, job.ID, "asr")
	if row.Status != "quota_exhausted" {
		t.Errorf("stage status = %q, want quota_exhausted", row.Status)
	}
	// The assertion this test exists for.
	if row.Attempt != 1 {
		t.Errorf("quota exhaustion consumed an attempt: asr attempt = %d, want 1", row.Attempt)
	}

	// And nothing re-dispatches while resume_after is in the future — a
	// parked job that got re-dispatched immediately would hammer a
	// provider that has already said no.
	time.Sleep(1500 * time.Millisecond)
	if n := h.rdb.XLen(h.ctx, queue.StreamASR).Val(); n != 1 {
		t.Errorf("a parked job must not be re-dispatched before resume_after; stage.asr holds %d envelopes", n)
	}
	if h.job(t, job.ID).State != string(StateASRQueued) {
		t.Error("the parked job left its queued state before resume_after elapsed")
	}

	// Once the park expires it resumes automatically, still at attempt 2 of
	// a full budget rather than having spent one on the cap.
	if _, err := h.pool.Exec(h.ctx, `UPDATE jobs SET resume_after = now() - interval '1 minute' WHERE id = $1`, job.ID); err != nil {
		t.Fatalf("expire the park: %v", err)
	}
	env2, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 15*time.Second)
	if env2.GetAttempt() != 2 {
		t.Errorf("resumed dispatch should be attempt 2, got %d", env2.GetAttempt())
	}
	h.publishResult(t, env2, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)
	h.waitForJobState(t, job.ID, 15*time.Second, StateASRDone, StateRedactRunning)
}
