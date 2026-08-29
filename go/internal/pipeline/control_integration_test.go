//go:build integration

package pipeline

import (
	"context"
	"testing"
	"time"

	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// TestIntegration_CancellationStopsDispatchAndKeepsCompletedArtifacts covers
// §4.4. Cancellation is cooperative: go-api sets the flag, the orchestrator
// stops dispatching and moves the job. Artifacts from completed stages are
// retained — they remain valid inputs for a later re-run, so cancelling and
// re-submitting does not re-pay for what already finished.
func TestIntegration_CancellationStopsDispatchAndKeepsCompletedArtifacts(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-cancel", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	// Finish ASR so there is a completed stage to protect.
	asrEnv, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	transcriptRef := h.artifactKeyFor(job, "asr", "transcript")
	h.publishResult(t, asrEnv, codev1.Status_STATUS_OK, transcriptRef, nil, nil)

	// Redact starts; the user cancels mid-stage.
	redactEnv, _ := h.readEnvelope(t, queue.StreamNLP, "nlp-worker", 10*time.Second)
	h.waitForJobState(t, job.ID, 10*time.Second, StateRedactRunning)

	if _, err := h.queries.RequestConsultationCancel(h.ctx, job.ConsultationID); err != nil {
		t.Fatalf("request cancel: %v", err)
	}

	final := h.waitForJobState(t, job.ID, 15*time.Second, StateCancelled)
	if final.State != string(StateCancelled) {
		t.Fatalf("job should be cancelled, is %q", final.State)
	}

	// The in-flight stage is marked cancelled, the completed one is not
	// touched.
	if got := h.stage(t, job.ID, "redact").Status; got != "cancelled" {
		t.Errorf("in-flight redact stage should be cancelled, is %q", got)
	}
	asr := h.stage(t, job.ID, "asr")
	if asr.Status != "succeeded" {
		t.Errorf("a completed stage must survive cancellation, asr is %q", asr.Status)
	}
	if asr.ResultRef.String != transcriptRef {
		t.Errorf("cancellation discarded a completed stage's result_ref: %q", asr.ResultRef.String)
	}
	artifact, err := h.queries.GetArtifactByURI(h.ctx, transcriptRef)
	if err != nil {
		t.Fatalf("a completed stage's artifact must be retained through cancellation (§4.4): %v", err)
	}
	if artifact.Uri != transcriptRef {
		t.Errorf("retained artifact uri = %q, want %q", artifact.Uri, transcriptRef)
	}

	// Nothing further is dispatched.
	nlpDepthBefore := h.rdb.XLen(h.ctx, queue.StreamNLP).Val()
	time.Sleep(1500 * time.Millisecond)
	if after := h.rdb.XLen(h.ctx, queue.StreamNLP).Val(); after != nlpDepthBefore {
		t.Errorf("cancelled job kept dispatching: stage.nlp grew from %d to %d", nlpDepthBefore, after)
	}

	// A late CANCELLED result from the worker that finally noticed the flag
	// must be absorbed without upsetting the terminal state.
	h.publishResult(t, redactEnv, codev1.Status_STATUS_CANCELLED, "", &codev1.Error{
		Code: "CANCELLED", Message: "cancel_requested observed at checkpoint",
	}, nil)
	time.Sleep(1500 * time.Millisecond)
	if got := h.job(t, job.ID).State; got != string(StateCancelled) {
		t.Errorf("a late CANCELLED result moved the job out of its terminal state: %q", got)
	}
}

// TestIntegration_ConsentRevocationHaltsProcessingMidPipeline covers §7.2's
// "the check is re-evaluated at every stage dispatch, not only at upload, so
// a mid-pipeline revocation halts processing".
//
// This is the DPDP control the report claims, so it is tested rather than
// assumed: the interesting case is revocation landing *between* two stages,
// where an upload-time-only check would happily dispatch the next one.
func TestIntegration_ConsentRevocationHaltsProcessingMidPipeline(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-consent", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	asrEnv, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)

	// Consent is withdrawn while ASR is in flight.
	if _, err := h.queries.RevokeConsentRecord(h.ctx, h.consent.ID); err != nil {
		t.Fatalf("revoke consent: %v", err)
	}

	// The ASR result still lands — the work was already done and its tokens
	// already spent, so recording it is honest.
	h.publishResult(t, asrEnv, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)

	// But redaction — and therefore NLP, and therefore any transmission to
	// a third-party LLM — must never be dispatched.
	final := h.waitForJobState(t, job.ID, 15*time.Second, StateCancelled)
	if final.State != string(StateCancelled) {
		t.Fatalf("a job whose consent was revoked must halt, is %q", final.State)
	}
	if n := h.rdb.XLen(h.ctx, queue.StreamNLP).Val(); n != 0 {
		t.Errorf("consent was revoked, yet %d messages reached stage.nlp", n)
	}
	if _, err := h.queries.GetJobStageByJobAndStage(h.ctx, jobStageKey(job.ID, "redact")); err == nil {
		t.Error("a redact stage row was created after consent was revoked")
	}
}

// TestIntegration_HeartbeatsReachJobStagesForSSE covers the progress path
// chosen for this build: workers publish StageHeartbeat to stage.progress,
// the orchestrator — the only sanctioned consumer of a stage.* stream
// (§1.2) — lands it on job_stages, and go-api's SSE endpoint reads Postgres
// (claude_context.md decision #35). go-api never touches Redis.
func TestIntegration_HeartbeatsReachJobStagesForSSE(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-progress", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "got_k2", true)
	env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	h.waitForJobState(t, job.ID, 10*time.Second, StateASRRunning)

	if _, err := h.q.PublishHeartbeat(h.ctx, &codev1.StageHeartbeat{
		JobId: job.ID.String(), Stage: env.GetStage(), Attempt: env.GetAttempt(),
		PercentComplete: 42.5, Step: "diarizing", TraceId: env.GetTraceId(),
	}); err != nil {
		t.Fatalf("publish heartbeat: %v", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		row := h.stage(t, job.ID, "asr")
		if row.PercentComplete > 0 {
			if row.PercentComplete != 42.5 {
				t.Errorf("percent_complete = %v, want 42.5", row.PercentComplete)
			}
			if row.Step.String != "diarizing" {
				t.Errorf("step = %q, want %q", row.Step.String, "diarizing")
			}
			if !row.HeartbeatAt.Valid {
				t.Error("heartbeat_at was not stamped — the stall detector reads it")
			}
			goto finished
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("heartbeat never reached job_stages within 10s")

finished:
	// A heartbeat arriving after the result must not resurrect a finished
	// stage or walk its percentage back down from 100. Reordering across
	// stage.progress and stage.results is legal — they are separate streams.
	h.publishResult(t, env, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)
	h.waitForJobState(t, job.ID, 15*time.Second, StateASRDone, StateRedactRunning)

	if _, err := h.q.PublishHeartbeat(h.ctx, &codev1.StageHeartbeat{
		JobId: job.ID.String(), Stage: env.GetStage(), Attempt: env.GetAttempt(),
		PercentComplete: 60, Step: "late-heartbeat", TraceId: env.GetTraceId(),
	}); err != nil {
		t.Fatalf("publish late heartbeat: %v", err)
	}
	time.Sleep(1500 * time.Millisecond)

	row := h.stage(t, job.ID, "asr")
	if row.Status != "succeeded" {
		t.Errorf("a late heartbeat changed a succeeded stage's status to %q", row.Status)
	}
	if row.PercentComplete != 100 {
		t.Errorf("a late heartbeat walked percent_complete back to %v", row.PercentComplete)
	}
}

// TestIntegration_ResumeJobRecomputesFromCompletedStages exercises §4.3's
// resume-from-last-successful-stage directly, for the case the dispatch scan
// cannot reach on its own: a job stranded in a running state.
func TestIntegration_ResumeJobRecomputesFromCompletedStages(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-resume", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	asrEnv, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	h.publishResult(t, asrEnv, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)
	redactEnv, _ := h.readEnvelope(t, queue.StreamNLP, "nlp-worker", 10*time.Second)
	h.publishResult(t, redactEnv, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "redact", "redaction_map"), nil, nil)
	h.waitForJobState(t, job.ID, 15*time.Second, StateNLPRunning, StateNLPQueued, StateRedacted)

	// Strand the job: mark it running with no live stage attempt, the shape
	// a crash between the stage write and the transition would leave.
	if _, err := h.pool.Exec(h.ctx,
		`UPDATE jobs SET state = 'asr_running' WHERE id = $1`, job.ID); err != nil {
		t.Fatalf("strand the job: %v", err)
	}

	if err := orch.ResumeJob(h.ctx, job.ID); err != nil {
		t.Fatalf("ResumeJob: %v", err)
	}

	// asr and redact both succeeded, so the resume point is nlp — not the
	// beginning. Re-running asr would re-pay its cost for an output that
	// already exists.
	got := State(h.job(t, job.ID).State)
	if got != StateNLPQueued {
		t.Fatalf("resume point = %q, want nlp_queued (asr and redact already succeeded)", got)
	}
	for _, stage := range []string{"asr", "redact"} {
		row := h.stage(t, job.ID, stage)
		if row.Status != "succeeded" || row.Attempt != 1 {
			t.Errorf("resume disturbed the completed %s stage: status=%q attempt=%d", stage, row.Status, row.Attempt)
		}
	}
}
