//go:build integration

package pipeline

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// TestIntegration_HappyPathRunsEveryStageInOrder is the baseline the other
// tests are deviations from: asr -> redact -> nlp -> awaiting_review, with
// redaction mandatory in the middle (§4.1, §7.3, ADR-0014).
func TestIntegration_HappyPathRunsEveryStageInOrder(t *testing.T) {
	h := newHarness(t)
	statter := &fakeStatter{}
	orch := h.newOrchestrator("orch-happy", statter)

	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "got_k2", true)

	// The worker side, stage by stage. Each read asserts the orchestrator
	// dispatched to the right stream: asr-service gets stage.asr; both
	// redact and nlp go to nlp-service on stage.nlp (§1.2 gives redaction
	// to nlp-service, and §2.1 defines only two request streams).
	for _, step := range []struct {
		stream string
		stage  codev1.Stage
		kind   string
	}{
		{queue.StreamASR, codev1.Stage_STAGE_ASR, "transcript"},
		{queue.StreamNLP, codev1.Stage_STAGE_REDACT, "redaction_map"},
		{queue.StreamNLP, codev1.Stage_STAGE_NLP, "clinical_note"},
	} {
		env, _ := h.readEnvelope(t, step.stream, "worker-1", 10*time.Second)
		if env.GetStage() != step.stage {
			t.Fatalf("expected %s on %s, got %s", step.stage, step.stream, env.GetStage())
		}
		if env.GetSchemaVersion() != queue.SchemaVersion {
			t.Errorf("envelope schema_version = %d, want %d", env.GetSchemaVersion(), queue.SchemaVersion)
		}
		if env.GetTraceId() != job.TraceID {
			t.Errorf("trace_id not propagated: envelope has %q, job has %q", env.GetTraceId(), job.TraceID)
		}
		if env.GetRunConfigId() != job.RunConfigID.String() {
			t.Errorf("envelope carries run_config_id %q, want %q", env.GetRunConfigId(), job.RunConfigID)
		}
		if env.GetPayloadRef() == "" {
			t.Error("envelope must carry a payload_ref, never inline data (ADR-0005)")
		}
		if env.GetDeadline() == nil || !env.GetDeadline().AsTime().After(time.Now()) {
			t.Error("envelope must carry an absolute future deadline (§2.2, §4.2)")
		}
		h.publishResult(t, env, codev1.Status_STATUS_OK, h.artifactKeyFor(job, stageName(step.stage), step.kind), nil, nil)
	}

	final := h.waitForJobState(t, job.ID, 15*time.Second, StateAwaitingReview)
	if final.State != string(StateAwaitingReview) {
		t.Fatalf("job ended in %q", final.State)
	}

	// Every stage recorded, succeeded, with its result_ref.
	rows := h.stages(t, job.ID)
	if len(rows) != 3 {
		t.Fatalf("expected 3 job_stages rows, got %d", len(rows))
	}
	for _, row := range rows {
		if row.Status != "succeeded" {
			t.Errorf("stage %s is %q, want succeeded", row.Stage, row.Status)
		}
		if !row.ResultRef.Valid || row.ResultRef.String == "" {
			t.Errorf("stage %s has no result_ref", row.Stage)
		}
		if row.PercentComplete != 100 {
			t.Errorf("succeeded stage %s shows %v%% complete", row.Stage, row.PercentComplete)
		}
	}

	// Artifacts indexed, so GET /consultations/{id}/result can resolve them.
	artifacts, err := h.queries.ListArtifactsByConsultation(h.ctx, sqlc.ListArtifactsByConsultationParams{
		ConsultationID: job.ConsultationID,
		RunConfigID:    pgtype.UUID{Bytes: job.RunConfigID, Valid: true},
	})
	if err != nil {
		t.Fatalf("list artifacts: %v", err)
	}
	if len(artifacts) != 3 {
		t.Errorf("expected 3 indexed artifacts, got %d", len(artifacts))
	}

	// §4.1: every transition is audited, in the transaction that performed
	// it. Asserted as an ordered subsequence rather than a count — the three
	// *_running entries are optional (dispatch skips its mark-running write
	// when the worker's result beats it there, see dispatchJob step 3), but
	// the three advances cannot be skipped and cannot be reordered.
	audited := h.auditedStates(t, job.ID)
	want := []string{string(StateASRDone), string(StateRedacted), string(StateAwaitingReview)}
	i := 0
	for _, got := range audited {
		if i < len(want) && got == want[i] {
			i++
		}
	}
	if i != len(want) {
		t.Errorf("audit_log is missing pipeline advances: want the subsequence %v, recorded %v", want, audited)
	}
	if len(audited) == 0 || audited[len(audited)-1] != string(StateAwaitingReview) {
		t.Errorf("the last audited transition should be into awaiting_review, got %v", audited)
	}

	// The consultation mirrors the job that feeds the review UI.
	c, err := h.queries.GetConsultation(h.ctx, job.ConsultationID)
	if err != nil {
		t.Fatalf("get consultation: %v", err)
	}
	if c.State != string(StateAwaitingReview) {
		t.Errorf("consultation state = %q, want awaiting_review", c.State)
	}
}

// TestIntegration_CrashMidStageThenRestartResumesWithoutDuplicatingWork is
// plan.md Phase 3 acceptance criterion 1.
//
// The scenario: the orchestrator dispatches ASR and is killed. While it is
// down the worker finishes and publishes its result, so the message is
// sitting unread on stage.results. A *fresh* orchestrator process starts.
//
// It must (a) pick the result up and advance, and (b) not re-run ASR — the
// expensive part. Re-running a completed stage would re-pay its token cost
// against a daily quota for an output that already exists (§4.3, ADR-0007).
func TestIntegration_CrashMidStageThenRestartResumesWithoutDuplicatingWork(t *testing.T) {
	h := newHarness(t)
	job := h.submitJob(t, "baseline", false)

	// --- first orchestrator: dispatches ASR, then dies ---
	ctx1, kill1 := context.WithCancel(h.ctx)
	orch1 := h.newOrchestrator("orch-crash-1", &fakeStatter{})
	go func() { _ = orch1.Run(ctx1) }()

	env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	h.waitForJobState(t, job.ID, 10*time.Second, StateASRRunning)

	kill1() // crash, mid-stage
	time.Sleep(300 * time.Millisecond)

	// The worker completes anyway and publishes. Nothing is consuming
	// stage.results right now — this is the message the dead orchestrator
	// never saw.
	transcriptRef := h.artifactKeyFor(job, "asr", "transcript")
	h.publishResult(t, env, codev1.Status_STATUS_OK, transcriptRef, nil, nil)

	// --- second orchestrator: a genuinely new process ---
	//
	// ReclaimMinIdle is shortened so the test does not wait out
	// controlStreamReclaimIdle. It has to be reclaimed rather than merely
	// read: a killed orchestrator can leave a result already delivered into
	// its own consumer's PEL — Redis had handed it over before the process
	// went away — and a plain XREADGROUP with ">" will never see it again.
	// That is exactly the case this test is here to cover.
	ctx2, kill2 := context.WithCancel(h.ctx)
	defer kill2()
	orch2 := h.newOrchestrator("orch-crash-2", &fakeStatter{}, func(c *Config) {
		c.ControlReclaimMinIdle = 300 * time.Millisecond
	})
	go func() { _ = orch2.Run(ctx2) }()

	// It consumes the pending result and moves the job on to redact.
	h.waitForJobState(t, job.ID, 15*time.Second, StateRedactRunning, StateASRDone)

	asr := h.stage(t, job.ID, "asr")
	if asr.Status != "succeeded" {
		t.Fatalf("asr should be succeeded after the restart, is %q", asr.Status)
	}
	if asr.ResultRef.String != transcriptRef {
		t.Errorf("asr result_ref = %q, want %q", asr.ResultRef.String, transcriptRef)
	}
	// The load-bearing assertion: ASR was attempted exactly once. A restart
	// that re-dispatched it would show attempt 2 here.
	if asr.Attempt != 1 {
		t.Errorf("asr attempt = %d, want 1 — the restarted orchestrator re-ran a completed stage", asr.Attempt)
	}

	// And no second ASR envelope was published. One extra XADD would be a
	// duplicate delivery of a ~33K-token stage.
	if extra := h.rdb.XLen(h.ctx, queue.StreamASR).Val(); extra != 1 {
		t.Errorf("stage.asr holds %d envelopes, want exactly 1 — the restart re-dispatched a completed stage", extra)
	}

	// The pipeline genuinely continues rather than merely not crashing.
	redactEnv, _ := h.readEnvelope(t, queue.StreamNLP, "nlp-worker", 10*time.Second)
	if redactEnv.GetStage() != codev1.Stage_STAGE_REDACT {
		t.Errorf("after resuming, the next stage should be redact, got %s", redactEnv.GetStage())
	}
	if redactEnv.GetPayloadRef() != transcriptRef {
		t.Errorf("redact should read the asr output %q, got %q", transcriptRef, redactEnv.GetPayloadRef())
	}
}

// TestIntegration_DuplicateResultDeliveryIsAbsorbed covers ADR-0007's core
// claim. At-least-once delivery means the same result can arrive twice; the
// second one must change nothing.
func TestIntegration_DuplicateResultDeliveryIsAbsorbed(t *testing.T) {
	h := newHarness(t)
	statter := &fakeStatter{}
	orch := h.newOrchestrator("orch-dup", statter)
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)
	env, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)

	ref := h.artifactKeyFor(job, "asr", "transcript")
	h.publishResult(t, env, codev1.Status_STATUS_OK, ref, nil, nil)
	h.waitForJobState(t, job.ID, 10*time.Second, StateASRDone, StateRedactRunning)

	before := h.stage(t, job.ID, "asr")
	beforeAudit := h.auditCountForJob(t, job.ID)
	beforeState := h.job(t, job.ID).State

	// The same result again — a worker that crashed before XACK, or a
	// reclaim sweep re-delivering.
	h.publishResult(t, env, codev1.Status_STATUS_OK, ref, nil, nil)
	// And a third time, for good measure.
	h.publishResult(t, env, codev1.Status_STATUS_OK, ref, nil, nil)
	time.Sleep(2 * time.Second)

	after := h.stage(t, job.ID, "asr")
	if after.Attempt != before.Attempt {
		t.Errorf("duplicate result changed the attempt count: %d -> %d", before.Attempt, after.Attempt)
	}
	if len(errorHistory(t, after)) != 0 {
		t.Errorf("duplicate OK result appended to error_history: %v", errorHistory(t, after))
	}
	if !after.FinishedAt.Valid || !after.FinishedAt.Time.Equal(before.FinishedAt.Time) {
		t.Errorf("duplicate result rewrote finished_at: %v -> %v", before.FinishedAt.Time, after.FinishedAt.Time)
	}

	// The job did not advance twice. It may have moved on from asr_done to
	// redact_running through normal dispatch, but it must not be past
	// redact.
	current := State(h.job(t, job.ID).State)
	if current != StateASRDone && current != StateRedactRunning {
		t.Errorf("job advanced past redact on a duplicate asr result: %q (was %q)", current, beforeState)
	}

	// Exactly one artifacts row: the UNIQUE constraint on uri means the
	// duplicate upserted rather than inserting a second.
	artifacts, err := h.queries.ListArtifactsByConsultation(h.ctx, sqlc.ListArtifactsByConsultationParams{
		ConsultationID: job.ConsultationID,
		RunConfigID:    pgtype.UUID{Bytes: job.RunConfigID, Valid: true},
	})
	if err != nil {
		t.Fatalf("list artifacts: %v", err)
	}
	transcripts := 0
	for _, a := range artifacts {
		if a.Kind == "transcript" {
			transcripts++
		}
	}
	if transcripts != 1 {
		t.Errorf("expected exactly 1 transcript artifact after duplicate delivery, got %d", transcripts)
	}

	// Duplicates must not spam the audit log with re-transitions either.
	// Some growth is legitimate (asr_done -> redact_running is normal
	// forward progress); a second full advance would add far more.
	if grew := h.auditCountForJob(t, job.ID) - beforeAudit; grew > 2 {
		t.Errorf("duplicate results produced %d extra audit transitions", grew)
	}

	// stage.results is fully drained — every duplicate was acknowledged,
	// not left cycling in the PEL.
	if n := h.pendingCount(t, queue.StreamResults); n != 0 {
		t.Errorf("%d result messages left pending after duplicate delivery", n)
	}
}

// TestIntegration_StageTimeoutRetriesThenDeadLetters covers §4.2's soft
// deadline and §2.5's max-attempt policy in one pass: redact has a 60s
// deadline and MaxAttempts 2, so a stage that never reports is retried once
// and then dead-lettered.
//
// The deadline is forced past by writing deadline_at into the row rather
// than waiting 60 seconds of real time — the behaviour under test is the
// orchestrator's reaction to an elapsed deadline, not the clock.
func TestIntegration_StageTimeoutRetriesThenDeadLetters(t *testing.T) {
	h := newHarness(t)
	orch := h.newOrchestrator("orch-timeout", &fakeStatter{})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	// Get through ASR so the job is on redact, the shortest-policy stage.
	asrEnv, _ := h.readEnvelope(t, queue.StreamASR, "asr-worker", 10*time.Second)
	h.publishResult(t, asrEnv, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)

	// Attempt 1: the worker takes the message and never reports.
	redactEnv, _ := h.readEnvelope(t, queue.StreamNLP, "nlp-worker", 10*time.Second)
	if redactEnv.GetStage() != codev1.Stage_STAGE_REDACT {
		t.Fatalf("expected redact, got %s", redactEnv.GetStage())
	}
	h.waitForJobState(t, job.ID, 10*time.Second, StateRedactRunning)
	h.expireStageDeadline(t, job.ID, "redact")

	// The reaper observes the elapsed deadline, records a RETRYABLE
	// failure, and backs the job off to its rest state for another try.
	h.waitForJobState(t, job.ID, 15*time.Second, StateASRDone, StateRedactRunning)

	// Attempt 2: same again. redact's MaxAttempts is 2, so this exhausts it.
	redactEnv2, _ := h.readEnvelope(t, queue.StreamNLP, "nlp-worker", 15*time.Second)
	if redactEnv2.GetAttempt() != 2 {
		t.Errorf("re-dispatch should be attempt 2, got %d", redactEnv2.GetAttempt())
	}
	h.waitForJobState(t, job.ID, 10*time.Second, StateRedactRunning)
	h.expireStageDeadline(t, job.ID, "redact")

	final := h.waitForJobState(t, job.ID, 20*time.Second, StateDeadLettered)
	if final.State != string(StateDeadLettered) {
		t.Fatalf("job should be dead_lettered after exhausting redact's 2 attempts, is %q", final.State)
	}

	row := h.stage(t, job.ID, "redact")
	if row.Attempt != 2 {
		t.Errorf("redact attempt = %d, want 2 (§2.5's max for redact)", row.Attempt)
	}
	history := errorHistory(t, row)
	if len(history) != 2 {
		t.Fatalf("error_history should hold one entry per failed attempt, got %d: %v", len(history), history)
	}
	for i, e := range history {
		errObj, _ := e["error"].(map[string]any)
		code, _ := errObj["code"].(string)
		if code != "DEADLINE_EXCEEDED" && code != "HEARTBEAT_LOST" {
			t.Errorf("attempt %d recorded error code %q, want a timeout code", i+1, code)
		}
	}
}

// expireStageDeadline pushes a running stage's deadline into the past so the
// reaper treats it as timed out, without the test waiting out a real
// per-stage window (60s for redact, up to 45 minutes for GoT NLP).
func (h *harness) expireStageDeadline(t *testing.T, jobID uuidLike, stage string) {
	t.Helper()
	tag, err := h.pool.Exec(h.ctx,
		`UPDATE job_stages
		 SET deadline_at = now() - interval '1 minute',
		     started_at  = now() - interval '1 hour'
		 WHERE job_id = $1 AND stage = $2 AND status = 'running'`,
		jobID, stage)
	if err != nil {
		t.Fatalf("expire deadline for %s: %v", stage, err)
	}
	if tag.RowsAffected() != 1 {
		t.Fatalf("expected exactly one running %s row to expire, updated %d", stage, tag.RowsAffected())
	}
}

// TestIntegration_WorkerDeathIsRecoveredByXAutoClaim is plan.md Phase 3's
// "an async job survives killing a worker mid-pipeline".
//
// The worker reads a message off stage.asr and dies without acknowledging
// it. The entry now sits in the asr-workers PEL, where — absent recovery —
// it would sit forever and that consultation would stop dead with no error
// anywhere. XAUTOCLAIM past the visibility timeout is what fixes it (§2.4).
func TestIntegration_WorkerDeathIsRecoveredByXAutoClaim(t *testing.T) {
	h := newHarness(t)
	// ReclaimMinIdle is dialled down so the real XAUTOCLAIM path runs
	// without the test sleeping out ASR's 40-minute visibility window. The
	// mechanism under test is unchanged — only how long an entry must look
	// idle before it qualifies.
	orch := h.newOrchestrator("orch-reclaim", &fakeStatter{}, func(c *Config) {
		c.ReclaimMinIdle = 300 * time.Millisecond
	})
	ctx, cancel := context.WithCancel(h.ctx)
	defer cancel()
	go func() { _ = orch.Run(ctx) }()

	job := h.submitJob(t, "baseline", false)

	// The worker takes the message — and that is the last anyone hears of
	// it. No XACK, no result, no heartbeat.
	env, msgID := h.readEnvelope(t, queue.StreamASR, "doomed-worker", 10*time.Second)
	h.waitForJobState(t, job.ID, 10*time.Second, StateASRRunning)

	if n := h.pendingCount(t, queue.StreamASR); n != 1 {
		t.Fatalf("expected the dead worker's entry to be pending, got %d pending", n)
	}

	// Let the entry go idle past the (shortened) visibility window so
	// XAUTOCLAIM qualifies it. Nothing else touches it — the worker is
	// gone.
	_ = msgID
	time.Sleep(600 * time.Millisecond)

	// Recovery: the reaper reclaims, records the stall as a retryable
	// failure, and the dispatch scan re-publishes at attempt 2.
	env2, _ := h.readEnvelope(t, queue.StreamASR, "healthy-worker", 20*time.Second)
	if env2.GetAttempt() != 2 {
		t.Errorf("re-dispatch after worker death should be attempt 2, got %d", env2.GetAttempt())
	}
	if env2.GetIdempotencyKey() != env.GetIdempotencyKey() {
		t.Errorf("a reclaimed stage must keep its idempotency key (§2.4): %q vs %q",
			env2.GetIdempotencyKey(), env.GetIdempotencyKey())
	}
	if env2.GetTraceId() != env.GetTraceId() {
		t.Errorf("a reclaimed stage must keep its trace_id (§2.4): %q vs %q", env2.GetTraceId(), env.GetTraceId())
	}

	// The healthy worker finishes and the pipeline carries on.
	h.publishResult(t, env2, codev1.Status_STATUS_OK, h.artifactKeyFor(job, "asr", "transcript"), nil, nil)
	h.waitForJobState(t, job.ID, 15*time.Second, StateASRDone, StateRedactRunning)

	row := h.stage(t, job.ID, "asr")
	if row.Status != "succeeded" {
		t.Errorf("asr should be succeeded, is %q", row.Status)
	}
	// One row, not two: the reclaimed work unit kept its idempotency key,
	// so the retry updated the same row rather than inserting a new one.
	if len(h.stages(t, job.ID)) > 2 { // asr + whatever redact has started
		t.Errorf("expected the retry to reuse the asr row, got %d stage rows", len(h.stages(t, job.ID)))
	}
	if len(errorHistory(t, row)) != 1 {
		t.Errorf("the worker's death should be recorded as exactly one failed attempt, got %v", errorHistory(t, row))
	}
}
