package pipeline

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"

	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// ReapOnce runs one recovery sweep (§2.4). It has two halves, because a
// worker can die in two distinguishable ways:
//
//	Redis half  — XAUTOCLAIM over stage.asr / stage.nlp finds entries a
//	              worker read but never acknowledged. The worker took the
//	              message and vanished.
//	Postgres half — ListStalledJobStages finds running stage rows past their
//	              soft deadline, or heartbeat-silent past the allowed gap.
//	              Catches the cases the PEL cannot: a worker that ACKed and
//	              then died, a message trimmed out of the stream, a stage
//	              that hung without dying.
//
// Both halves converge on failStalledStage, which is idempotent per
// (stage row, attempt) — so the two halves firing on the same stage in the
// same sweep costs one recorded failure, not two.
func (o *Orchestrator) ReapOnce(ctx context.Context) error {
	var firstErr error
	if err := o.reclaimAbandonedMessages(ctx); err != nil {
		o.logger.ErrorContext(ctx, "orchestrator: reclaiming abandoned messages failed", "error", err)
		firstErr = err
	}
	if err := o.reapStalledStages(ctx); err != nil {
		o.logger.ErrorContext(ctx, "orchestrator: reaping stalled stages failed", "error", err)
		if firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// reclaimAbandonedMessages is §2.4's "issues XAUTOCLAIM against each stream
// for entries idle longer than the stage's visibility timeout, then
// re-dispatches with attempt + 1".
//
// The visibility timeout used is the *longest* of the stages that share the
// stream, not the per-message one: MinIdle is a property of the XAUTOCLAIM
// call, and stage.nlp carries both redact (2 min) and nlp-GoT (90 min). If
// the shorter one were used, the reaper would reclaim a healthy 40-minute
// GoT stage out from under its worker and re-dispatch it — spending ~33K
// tokens of a 100K daily budget on work already in progress. Erring long
// costs recovery latency for redact; erring short costs the research
// budget, so the per-stage deadline check in reapStalledStages carries the
// tighter bound instead.
func (o *Orchestrator) reclaimAbandonedMessages(ctx context.Context) error {
	streams := []struct {
		stream  string
		group   string
		minIdle time.Duration
	}{
		{queue.StreamASR, queue.GroupASRWorkers, queue.PolicyFor(codev1.Stage_STAGE_ASR, queue.PolicyOptions{LocalASR: true}).VisibilityTimeout},
		{queue.StreamNLP, queue.GroupNLPWorkers, queue.PolicyFor(codev1.Stage_STAGE_NLP, queue.PolicyOptions{GoTArm: true}).VisibilityTimeout},
	}

	for _, s := range streams {
		minIdle := s.minIdle
		if o.cfg.ReclaimMinIdle > 0 {
			minIdle = o.cfg.ReclaimMinIdle
		}
		msgs, err := o.q.ReclaimStale(ctx, s.stream, s.group, o.cfg.ConsumerName, minIdle, 32)
		if err != nil {
			return err
		}
		for _, msg := range msgs {
			env, err := queue.DecodeEnvelope(msg)
			if err != nil {
				o.logger.WarnContext(ctx, "orchestrator: reclaimed an undecodable envelope, acknowledging it",
					"stream", s.stream, "message_id", msg.ID, "error", err)
				_ = o.q.Ack(ctx, s.stream, s.group, msg.ID)
				continue
			}
			jobID, err := uuid.Parse(env.GetJobId())
			if err != nil {
				_ = o.q.Ack(ctx, s.stream, s.group, msg.ID)
				continue
			}

			o.logger.WarnContext(ctx, "orchestrator: reclaiming a message abandoned by a dead worker",
				"stream", s.stream, "message_id", msg.ID, "job_id", jobID,
				"stage", stageName(env.GetStage()), "attempt", env.GetAttempt(), "trace_id", env.GetTraceId())
			o.metrics.RecordReclaim(stageName(env.GetStage()))

			if err := o.failStalledStage(ctx, jobID, env.GetStage(), "WORKER_STALLED",
				fmt.Sprintf("no acknowledgement within the %s visibility timeout for stage %s", minIdle, stageName(env.GetStage()))); err != nil {
				// Left unacknowledged so the next sweep retries: the
				// reclaim has no durable effect until the stage row moves.
				o.logger.ErrorContext(ctx, "orchestrator: recording a reclaimed stage failure failed",
					"job_id", jobID, "message_id", msg.ID, "error", err)
				continue
			}

			// Acknowledged only now. The re-dispatch publishes a *fresh*
			// envelope at attempt+1, so leaving this entry pending would
			// simply move the leak from the dead worker's PEL into this
			// orchestrator's own.
			if err := o.q.Ack(ctx, s.stream, s.group, msg.ID); err != nil {
				o.logger.WarnContext(ctx, "orchestrator: acking a reclaimed message failed", "message_id", msg.ID, "error", err)
			}
		}
	}
	return nil
}

// reapStalledStages enforces the per-stage soft deadline (§4.2) and §2.4's
// "a job whose heartbeats stopped more than 90s ago is considered stalled
// even if inside the visibility window".
func (o *Orchestrator) reapStalledStages(ctx context.Context) error {
	rows, err := o.queries.ListStalledJobStages(ctx, sqlc.ListStalledJobStagesParams{
		Limit:               o.cfg.DispatchBatch,
		HeartbeatGapSeconds: queue.HeartbeatStallThreshold.Seconds(),
		StartupGraceSeconds: queue.StartupGrace.Seconds(),
	})
	if err != nil {
		return fmt.Errorf("pipeline: scan stalled stages: %w", err)
	}
	for _, row := range rows {
		stage, err := queue.StageFromName(row.Stage)
		if err != nil {
			continue
		}
		reason, detail := "DEADLINE_EXCEEDED", fmt.Sprintf("stage %s exceeded its soft deadline", row.Stage)
		if row.DeadlineAt.Valid && row.DeadlineAt.Time.After(time.Now()) {
			reason, detail = "HEARTBEAT_LOST", fmt.Sprintf("stage %s stopped heartbeating while still inside its deadline", row.Stage)
		}
		o.logger.WarnContext(ctx, "orchestrator: stage stalled",
			"job_id", row.JobID, "stage", row.Stage, "attempt", row.Attempt,
			"reason", reason, "deadline_at", row.DeadlineAt.Time, "heartbeat_at", row.HeartbeatAt.Time)
		o.metrics.RecordTimeout(row.Stage)

		if err := o.failStalledStage(ctx, row.JobID, stage, reason, detail); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: recording a stalled stage failed", "job_id", row.JobID, "error", err)
		}
	}
	return nil
}

// failStalledStage records a stall as a RETRYABLE failure and lets the
// normal retry/DLQ policy decide what happens next — a stalled stage is
// exactly what §4.2's "a worker past its deadline aborts and returns
// RETRYABLE / DEADLINE_EXCEEDED" describes, arriving via the orchestrator's
// own observation rather than the worker's, because a worker that died
// cannot report its own death.
//
// It is idempotent per (stage row, attempt): the row is re-read under the
// job lock and skipped unless it is still 'running'. That is what lets the
// XAUTOCLAIM half and the deadline half both fire on the same stage without
// double-counting an attempt.
func (o *Orchestrator) failStalledStage(ctx context.Context, jobID uuid.UUID, stage codev1.Stage, code, message string) error {
	var dlq *queue.DeadLetterRequest

	if err := o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}
		if IsTerminal(State(job.State)) {
			return nil
		}
		row, err := q.GetJobStageByJobAndStage(ctx, sqlc.GetJobStageByJobAndStageParams{
			JobID: jobID, Stage: stageName(stage),
		})
		if err != nil {
			return fmt.Errorf("pipeline: load stage %s of job %s: %w", stageName(stage), jobID, err)
		}
		if row.Status != "running" {
			// Already resolved — the real result landed between the sweep
			// and this lock, or the other half of the reaper got here
			// first.
			return nil
		}

		res := &codev1.StageResult{
			JobId: jobID.String(), Stage: stage, Attempt: uint32(row.Attempt), //nolint:gosec // bounded by MaxAttempts
			IdempotencyKey: row.IdempotencyKey,
			Status:         codev1.Status_STATUS_RETRYABLE,
			Error:          &codev1.Error{Code: code, Message: message, Retryable: true},
		}
		return o.applyFailure(ctx, q, job, row, res, false, &dlq)
	}); err != nil {
		return err
	}

	if dlq != nil {
		if _, err := o.q.PublishDeadLetter(ctx, *dlq); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: publishing dead letter for a stalled stage failed",
				"job_id", jobID, "stage", stageName(stage), "error", err)
		} else {
			o.metrics.RecordDeadLetter(stageName(stage))
		}
	}
	return nil
}

// ResumeJob recomputes a job's position from its completed stages and moves
// it there — §4.3's "a crash mid-pipeline resumes at the last incomplete
// stage".
//
// Normal recovery does not need this: a crashed orchestrator's jobs are
// already in states the dispatch scan and reaper handle. It exists for the
// case those cannot reach — a job left in a *_running state whose stage row
// was never written, or one an operator wants to re-drive after fixing the
// cause of a dead-letter — and it never re-runs a succeeded stage, because
// resumeState stops at the first stage without one.
func (o *Orchestrator) ResumeJob(ctx context.Context, jobID uuid.UUID) error {
	return o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}
		stages, err := q.ListJobStagesByJob(ctx, jobID)
		if err != nil {
			return fmt.Errorf("pipeline: list stages of job %s: %w", jobID, err)
		}
		succeeded := make(map[codev1.Stage]bool, len(stages))
		for _, s := range stages {
			if s.Status != "succeeded" {
				continue
			}
			if stage, err := queue.StageFromName(s.Stage); err == nil {
				succeeded[stage] = true
			}
		}
		target := resumeState(succeeded)
		if State(job.State) == target {
			return nil
		}
		o.logger.InfoContext(ctx, "orchestrator: resuming job from its last successful stage",
			"job_id", jobID, "from", job.State, "to", string(target), "succeeded_stages", len(succeeded))
		_, err = o.transition(ctx, q, job, target, job.CurrentStage, job.Attempt, nil, nil)
		return err
	})
}
