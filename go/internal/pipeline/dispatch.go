package pipeline

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/types/known/timestamppb"

	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// DispatchOnce runs one dispatch scan: every job in a dispatchable state
// whose resume_after has elapsed, whose consultation is neither cancelled
// nor erased, and whose consent is still valid (§7.2 — consent is
// re-evaluated at every dispatch, not only at upload, so a mid-pipeline
// revocation halts processing).
//
// This scan is the whole of crash recovery. There is no separate "resume"
// path, because a job's state *is* the resume point: an orchestrator that
// died between any two steps left the job either in a rest state (the scan
// picks it up) or in a running state with a stalled stage (the reaper
// returns it to a rest state, and then the scan picks it up).
func (o *Orchestrator) DispatchOnce(ctx context.Context) error {
	jobs, err := o.queries.ListDispatchableJobs(ctx, sqlc.ListDispatchableJobsParams{
		Limit:  o.cfg.DispatchBatch,
		States: DispatchableStateNames(),
	})
	if err != nil {
		return fmt.Errorf("pipeline: scan dispatchable jobs: %w", err)
	}
	for _, job := range jobs {
		if err := o.dispatchJob(ctx, job.ID); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: dispatch failed", "job_id", job.ID, "error", err)
		}
	}
	return nil
}

// dispatchJob advances one job by one stage.
//
// The three steps are ordered so that a crash between any two of them is
// recoverable without a reaper:
//
//  1. persist the job_stages claim   (tx)
//  2. publish the envelope           (Redis, guarded by the publish key)
//  3. mark the job *_running         (tx)
//
// Crash after 1: the job is still in its rest state, so the next scan
// re-runs all three. Crash after 2: the same — and the publish guard
// recognises the (key, attempt) triple and skips the duplicate XADD, so
// re-running costs nothing. The alternative ordering (mark running, then
// publish) has a hole: a crash in between leaves a job marked running that
// no message exists for, recoverable only by waiting out a stage timeout.
func (o *Orchestrator) dispatchJob(ctx context.Context, jobID uuid.UUID) error {
	var (
		env       *codev1.StageEnvelope
		fromRest  State
		toRunning State
		stage     codev1.Stage
		skip      bool
	)

	if err := o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}

		// Re-read state under the row lock. The scan's snapshot may be
		// stale by the time the lock is granted — a result message for
		// this job could have landed in between — and dispatching from a
		// stale state is how a stage gets run twice.
		st, ok := stageForState(State(job.State))
		if !ok {
			skip = true
			return nil
		}
		stage = st
		fromRest = State(job.State)

		if halt, reason, err := o.consultationHalted(ctx, q, job); err != nil {
			return err
		} else if halt {
			skip = true
			return o.haltJob(ctx, q, job, reason)
		}

		cfg, err := o.runConfigFor(ctx, q, job)
		if err != nil {
			return err
		}
		policy := queue.PolicyFor(stage, queue.PolicyOptionsFor(cfg))

		inputRef, inputSHA, err := o.stageInput(ctx, q, job, stage)
		if err != nil {
			return err
		}
		key := queue.IdempotencyKey(job.ConsultationID, stage, job.RunConfigID, inputSHA)

		// Attempts are counted per *stage*, not per job: §2.5's max-attempt
		// ceiling is per stage (asr 3, redact 2, nlp 3), so a job that used
		// two ASR attempts must still get redact's full two. The counter
		// therefore comes from the existing job_stages row for this work
		// unit, not from jobs.attempt — which mirrors the current stage's
		// count so GET /v1/jobs/{id} can display it without a join.
		//
		// That row is keyed by idempotency_key, not job_id — RunConfig
		// interning (ADR-0012) means a DIFFERENT job (a resubmit after this
		// consultation's prior job dead-lettered or failed without this
		// stage ever succeeding) can land on the exact same key. Continuing
		// prev.Attempt+1 in that case would hand the new job its
		// predecessor's already-exhausted attempt budget, dead-lettering it
		// before it ever ran — this is that job's own first attempt at this
		// stage, regardless of what the row's previous owner used up.
		attempt := int32(1)
		if prev, err := q.GetJobStageByIdempotencyKey(ctx, key); err == nil {
			if prev.JobID == job.ID {
				attempt = prev.Attempt + 1
			}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("pipeline: read prior attempt for stage %s: %w", stageName(stage), err)
		}
		deadline := time.Now().Add(policy.SoftDeadline)

		stageRow, err := q.DispatchJobStage(ctx, sqlc.DispatchJobStageParams{
			JobID:          job.ID,
			Stage:          stageName(stage),
			IdempotencyKey: key,
			Attempt:        attempt,
			DeadlineAt:     timestamptzOrNull(&deadline),
		})
		if err != nil {
			if !errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("pipeline: claim stage %s for job %s: %w", stageName(stage), job.ID, err)
			}
			// No rows means the DO UPDATE ... WHERE status <> 'succeeded'
			// guard matched nothing: this exact work unit already
			// succeeded. This is the case §4.3 is about — a crash mid-
			// pipeline resumes at the last incomplete stage, and a
			// completed stage is never recomputed. Advance past it using
			// the stored result instead of re-dispatching.
			skip = true
			return o.advancePastCompletedStage(ctx, q, job, stage, key)
		}

		env = &codev1.StageEnvelope{
			JobId:          job.ID.String(),
			ConsultationId: job.ConsultationID.String(),
			Stage:          stage,
			Attempt:        uint32(attempt), //nolint:gosec // attempt is bounded by MaxAttempts
			IdempotencyKey: key,
			TraceId:        job.TraceID,
			SchemaVersion:  queue.SchemaVersion,
			RunConfigId:    job.RunConfigID.String(),
			PayloadRef:     inputRef,
			EnqueuedAt:     timestamppb.Now(),
			Deadline:       timestamppb.New(deadline),
			Labels:         o.envelopeLabels(job, cfg),
		}
		st2, _ := stepFor(stage)
		toRunning = st2.Running
		_ = stageRow
		return nil
	}); err != nil {
		return err
	}
	if skip || env == nil {
		return nil
	}

	// Step 2 — publish. Outside the transaction on purpose: holding a row
	// lock across a network round trip to Redis would let one slow XADD
	// block every other write to that job.
	res, err := o.q.PublishEnvelope(ctx, env)
	if err != nil {
		// The job is still in its rest state, so the next scan retries the
		// whole sequence. Nothing is lost.
		return fmt.Errorf("pipeline: publish %s envelope for job %s: %w", stageName(stage), jobID, err)
	}
	if res.Deduplicated {
		o.logger.InfoContext(ctx, "orchestrator: envelope already published for this attempt, not re-publishing",
			"job_id", jobID, "stage", stageName(stage), "attempt", env.GetAttempt())
	}
	o.metrics.RecordDispatch(stageName(stage))

	// Step 3 — mark running.
	//
	// Only if the job is *still* in the rest state this dispatch started
	// from. A fast worker can publish its result, and the results consumer
	// can apply it, in the window between step 2 and here — the stage row
	// already exists as `running`, so nothing about that path waits for
	// this transition. Writing `running` unconditionally would then drag
	// the job backwards over a completed stage and strand it there until a
	// timeout, which is precisely what
	// TestIntegration_ResumeJobRecomputesFromCompletedStages caught.
	return o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}
		if State(job.State) != fromRest {
			o.logger.DebugContext(ctx, "orchestrator: job moved on before the running transition, leaving it alone",
				"job_id", jobID, "state", job.State, "expected", string(fromRest))
			return nil
		}
		name := stageName(stage)
		_, err = o.transition(ctx, q, job, toRunning, &name, int32(env.GetAttempt()), nil, nil) //nolint:gosec // bounded by MaxAttempts
		return err
	})
}

// advancePastCompletedStage moves a job forward over a stage that already
// has a succeeded row, without re-running it. Called when the dispatch
// claim finds the work unit already done — the resume-from-last-successful-
// stage path (§4.3).
func (o *Orchestrator) advancePastCompletedStage(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stage codev1.Stage, key string) error {
	existing, err := q.GetJobStageByIdempotencyKey(ctx, key)
	if err != nil {
		return fmt.Errorf("pipeline: load completed stage %s: %w", key, err)
	}
	next, ok := nextAfter(stage)
	if !ok {
		return fmt.Errorf("pipeline: stage %s has no successor", stageName(stage))
	}
	o.logger.InfoContext(ctx, "orchestrator: stage already succeeded, resuming past it",
		"job_id", job.ID, "stage", stageName(stage), "result_ref", existing.ResultRef.String)
	name := stageName(stage)
	_, err = o.transition(ctx, q, job, next, &name, job.Attempt, nil, nil)
	return err
}

// stageInput resolves what a stage reads, as an object key plus the content
// hash that goes into its idempotency key (§2.3).
//
// Each stage's input is the previous stage's durable output — which is what
// makes stage boundaries the checkpoints §4.3 describes. ASR is the base
// case: its input is the immutable source audio recorded on the
// consultation row, content-addressed at upload (claude_context.md
// decision #36).
func (o *Orchestrator) stageInput(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stage codev1.Stage) (ref string, sha string, err error) {
	if stage == codev1.Stage_STAGE_ASR {
		c, err := q.GetConsultation(ctx, job.ConsultationID)
		if err != nil {
			return "", "", fmt.Errorf("pipeline: load consultation %s: %w", job.ConsultationID, err)
		}
		if !c.SourceAudioUri.Valid || !c.AudioSha256.Valid {
			return "", "", fmt.Errorf("pipeline: consultation %s has no confirmed source audio", job.ConsultationID)
		}
		return c.SourceAudioUri.String, c.AudioSha256.String, nil
	}

	prev, ok := previousStage(stage)
	if !ok {
		return "", "", fmt.Errorf("pipeline: stage %s has no predecessor to read from", stageName(stage))
	}
	prevRow, err := q.GetJobStageByJobAndStage(ctx, sqlc.GetJobStageByJobAndStageParams{
		JobID: job.ID, Stage: stageName(prev),
	})
	if err != nil {
		return "", "", fmt.Errorf("pipeline: load %s result for job %s: %w", stageName(prev), job.ID, err)
	}
	if prevRow.Status != "succeeded" || !prevRow.ResultRef.Valid {
		return "", "", fmt.Errorf("pipeline: cannot dispatch %s for job %s — %s has not succeeded", stageName(stage), job.ID, stageName(prev))
	}

	// The predecessor's artifacts row carries the content hash. Falling
	// back to the result_ref itself keeps dispatch working when object
	// storage is not configured: the ref is content-partitioned by
	// run_config_id (§3.3), so it is still stable across redeliveries and
	// distinct across arms — which is all the idempotency key needs.
	if a, err := q.GetArtifactByURI(ctx, prevRow.ResultRef.String); err == nil && a.Sha256 != "" {
		return prevRow.ResultRef.String, a.Sha256, nil
	}
	return prevRow.ResultRef.String, prevRow.ResultRef.String, nil
}

func previousStage(stage codev1.Stage) (codev1.Stage, bool) {
	for i, st := range autoPipeline {
		if st.Stage == stage && i > 0 {
			return autoPipeline[i-1].Stage, true
		}
	}
	return codev1.Stage_STAGE_UNSPECIFIED, false
}

// envelopeLabels populates StageEnvelope.labels (§2.2: "eval_run_id, arm,
// org_id — for filtering"), so an eval sweep's stream traffic can be
// filtered without joining back to Postgres.
func (o *Orchestrator) envelopeLabels(job *sqlc.Job, cfg *codev1.RunConfig) map[string]string {
	labels := map[string]string{"arm": cfg.GetArm()}
	if job.EvalRunID.Valid {
		labels["eval_run_id"] = uuid.UUID(job.EvalRunID.Bytes).String()
	}
	return labels
}
