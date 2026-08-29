package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

// handleResultMessage is the stage.results handler.
//
// Returning nil acknowledges the message, so nil is returned only after
// everything the result implies is committed to Postgres (§2.3's
// acknowledgement rule). Returning an error leaves the entry pending, and
// the consumer's reclaim sweep retries it — the same path a crash takes.
func (o *Orchestrator) handleResultMessage(ctx context.Context, msg queue.Message) error {
	res, err := queue.DecodeResult(msg)
	if err != nil {
		if errors.Is(err, queue.ErrSchemaUnsupported) {
			// §2.2: a version this build does not implement is rejected,
			// never guessed at. Retrying cannot make an unimplemented
			// contract implementable, so it is acknowledged and recorded
			// rather than left to cycle through the PEL forever.
			o.logger.ErrorContext(ctx, "orchestrator: dropping result with unsupported schema version",
				"message_id", msg.ID, "job_id", msg.Field(queue.FieldJobID), "error", err)
			return nil
		}
		o.logger.ErrorContext(ctx, "orchestrator: undecodable result message", "message_id", msg.ID, "error", err)
		return nil
	}
	return o.ApplyResult(ctx, res)
}

// ApplyResult applies one StageResult to the state machine, transactionally
// and idempotently.
//
// Idempotency is the point of this function. At-least-once delivery
// (ADR-0007) means a result can arrive twice: the worker crashed after
// persisting but before XACK, or a reclaim sweep re-delivered it, or the
// worker itself re-emitted a stored result after its own dedupe check.
// Rather than trying to detect duplicates, this is written as a
// *convergent* operation — "make the database consistent with this result"
// — so applying it twice reaches the same state as applying it once.
//
// Concretely: a second OK for an already-succeeded stage does not
// re-complete the stage, does not re-append to error_history, does not
// double-count an attempt, and does not advance the job a second time. It
// does still finish an advance that a crash interrupted, which is why the
// job-state check is "is the job past this stage" rather than "have I seen
// this message".
func (o *Orchestrator) ApplyResult(ctx context.Context, res *codev1.StageResult) error {
	jobID, err := uuid.Parse(res.GetJobId())
	if err != nil {
		return fmt.Errorf("pipeline: result carries an unparseable job id %q: %w", res.GetJobId(), err)
	}

	var (
		dlq       *queue.DeadLetterRequest
		duplicate bool
	)

	if err := o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}
		stageRow, err := o.resolveStageRow(ctx, q, job, res)
		if err != nil {
			return err
		}

		// A result for a job the orchestrator has already finished with —
		// cancelled, dead-lettered, approved — changes nothing. Recorded
		// and acknowledged rather than applied.
		if IsTerminal(State(job.State)) {
			o.logger.InfoContext(ctx, "orchestrator: result for a terminal job, ignoring",
				"job_id", job.ID, "job_state", job.State, "stage", res.GetStage().String())
			duplicate = true
			return nil
		}

		switch res.GetStatus() {
		case codev1.Status_STATUS_OK:
			return o.applyOK(ctx, q, job, stageRow, res, &duplicate)
		case codev1.Status_STATUS_QUOTA_EXHAUSTED:
			return o.applyQuotaExhausted(ctx, q, job, stageRow, res)
		case codev1.Status_STATUS_CANCELLED:
			return o.applyCancelled(ctx, q, job, stageRow, res)
		case codev1.Status_STATUS_FATAL:
			return o.applyFailure(ctx, q, job, stageRow, res, true, &dlq)
		case codev1.Status_STATUS_RETRYABLE:
			return o.applyFailure(ctx, q, job, stageRow, res, false, &dlq)
		default:
			// An unspecified status is a worker bug. Treated as FATAL:
			// retrying a result the orchestrator cannot interpret would
			// loop, and guessing OK would advance the pipeline on an
			// unverified stage.
			o.logger.ErrorContext(ctx, "orchestrator: result with unspecified status, treating as fatal",
				"job_id", job.ID, "stage", res.GetStage().String())
			return o.applyFailure(ctx, q, job, stageRow, res, true, &dlq)
		}
	}); err != nil {
		return err
	}

	if duplicate {
		o.metrics.RecordDuplicateResult(stageName(res.GetStage()))
	}

	// The DeadLetter is published after the transaction commits. If this
	// XADD fails the job is already recorded as dead_lettered with its
	// full error_history in job_stages, so the message is reconstructible
	// from Postgres — which is exactly why error_history lives there and
	// not only in Redis.
	if dlq != nil {
		if _, err := o.q.PublishDeadLetter(ctx, *dlq); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: publishing dead letter failed — job is recorded as dead_lettered, replay it from job_stages.error_history",
				"job_id", res.GetJobId(), "stage", stageName(res.GetStage()), "error", err)
			return nil
		}
		o.metrics.RecordDeadLetter(stageName(res.GetStage()))
	}
	return nil
}

// resolveStageRow finds the job_stages row a result belongs to. The
// idempotency key is the primary lookup; (job, stage) is the fallback for a
// reclaimed message whose key predates an input change.
func (o *Orchestrator) resolveStageRow(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, res *codev1.StageResult) (*sqlc.JobStage, error) {
	if key := res.GetIdempotencyKey(); key != "" {
		row, err := q.GetJobStageByIdempotencyKey(ctx, key)
		if err == nil {
			return row, nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("pipeline: look up stage by idempotency key: %w", err)
		}
	}
	row, err := q.GetJobStageByJobAndStage(ctx, sqlc.GetJobStageByJobAndStageParams{
		JobID: job.ID, Stage: stageName(res.GetStage()),
	})
	if err != nil {
		return nil, fmt.Errorf("pipeline: no job_stages row for job %s stage %s: %w", job.ID, stageName(res.GetStage()), err)
	}
	return row, nil
}

func (o *Orchestrator) applyOK(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stageRow *sqlc.JobStage, res *codev1.StageResult, duplicate *bool) error {
	next, ok := nextAfter(res.GetStage())
	if !ok {
		return fmt.Errorf("pipeline: stage %s is not part of the automatic pipeline", stageName(res.GetStage()))
	}

	if stageRow.Status == "succeeded" {
		*duplicate = true
		// The stage is already recorded as done. The only thing left to
		// check is whether the *job* advance completed — a crash between
		// CompleteJobStage and TransitionJob would leave the stage
		// succeeded and the job still in its running state. Finishing that
		// advance here is what makes the operation convergent rather than
		// merely "skipped on redelivery".
		if State(job.State) == runningStateFor(res.GetStage()) {
			o.logger.InfoContext(ctx, "orchestrator: duplicate result completing an interrupted advance",
				"job_id", job.ID, "stage", stageName(res.GetStage()))
			name := stageName(res.GetStage())
			_, err := o.transition(ctx, q, job, next, &name, job.Attempt, nil, nil)
			return err
		}
		o.logger.InfoContext(ctx, "orchestrator: duplicate result for an already-completed stage, ignoring",
			"job_id", job.ID, "stage", stageName(res.GetStage()), "job_state", job.State)
		return nil
	}

	metrics, err := marshalMetrics(res.GetMetrics())
	if err != nil {
		return err
	}
	if _, err := q.CompleteJobStage(ctx, sqlc.CompleteJobStageParams{
		ID:        stageRow.ID,
		ResultRef: pgTextOrNull(res.GetResultRef()),
		Metrics:   metrics,
	}); err != nil {
		return fmt.Errorf("pipeline: complete stage %s: %w", stageName(res.GetStage()), err)
	}

	if err := o.indexArtifact(ctx, q, job, res); err != nil {
		// Indexing is a convenience over job_stages.result_ref, which is
		// already committed above. Failing the whole transaction over it
		// would turn a metadata problem into a stalled consultation.
		o.logger.WarnContext(ctx, "orchestrator: indexing stage artifact failed",
			"job_id", job.ID, "result_ref", res.GetResultRef(), "error", err)
	}

	o.metrics.RecordStageOutcome(stageName(res.GetStage()), "succeeded")
	name := stageName(res.GetStage())
	_, err = o.transition(ctx, q, job, next, &name, job.Attempt, nil, nil)
	return err
}

// applyQuotaExhausted parks the job. §2.5 calls this "the single most
// important operational detail of the whole system", and the reason is the
// attempt counter: on a free tier a daily cap is hit routinely, and if
// quota exhaustion counted as a retry a single cap would burn all three
// attempts within seconds and dead-letter every job in an eval sweep.
//
// So: state goes back to the stage's rest state, resume_after is set from
// the provider's retry-after, and attempt is left exactly as it was.
func (o *Orchestrator) applyQuotaExhausted(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stageRow *sqlc.JobStage, res *codev1.StageResult) error {
	resumeAfter := res.GetResumeAfter().AsTime()
	if !res.GetResumeAfter().IsValid() || resumeAfter.Before(time.Now()) {
		// No usable retry-after from the provider: wait out the rest of
		// the hour rather than spinning against a cap that has not lifted.
		resumeAfter = time.Now().Add(time.Hour)
	}

	if _, err := q.FailJobStage(ctx, sqlc.FailJobStageParams{
		ID:           stageRow.ID,
		Status:       "quota_exhausted",
		LastError:    marshalError(res.GetError()),
		AttemptError: attemptErrorJSON(stageRow.Attempt, res.GetError()),
		Metrics:      mustMetrics(res.GetMetrics()),
	}); err != nil {
		return fmt.Errorf("pipeline: record quota exhaustion for stage %s: %w", stageName(res.GetStage()), err)
	}

	o.metrics.RecordQuotaPark(stageName(res.GetStage()))
	o.logger.InfoContext(ctx, "orchestrator: quota exhausted, parking job without consuming an attempt",
		"job_id", job.ID, "stage", stageName(res.GetStage()), "attempt", job.Attempt, "resume_after", resumeAfter)

	name := stageName(res.GetStage())
	_, err := o.transition(ctx, q, job, parkState(res.GetStage()), &name, job.Attempt, &resumeAfter, marshalError(res.GetError()))
	return err
}

func (o *Orchestrator) applyCancelled(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stageRow *sqlc.JobStage, res *codev1.StageResult) error {
	if _, err := q.FailJobStage(ctx, sqlc.FailJobStageParams{
		ID:           stageRow.ID,
		Status:       "cancelled",
		LastError:    marshalError(res.GetError()),
		AttemptError: attemptErrorJSON(stageRow.Attempt, res.GetError()),
		Metrics:      mustMetrics(res.GetMetrics()),
	}); err != nil {
		return fmt.Errorf("pipeline: record cancellation for stage %s: %w", stageName(res.GetStage()), err)
	}
	name := stageName(res.GetStage())
	// Artifacts from completed stages are retained (§4.4) — they remain
	// valid inputs for a later re-run — so nothing is deleted here.
	_, err := o.transition(ctx, q, job, StateCancelled, &name, job.Attempt, nil, marshalError(res.GetError()))
	return err
}

// applyFailure handles RETRYABLE and FATAL.
//
// FATAL goes to the DLQ immediately, without consuming the remaining
// attempts, because §2.5 classifies it as unfixable by repetition:
// malformed input, an unsupported schema, a validation failure that will
// fail identically next time. RETRYABLE backs off and re-dispatches until
// the stage's max attempts (§4.2) is reached, then routes to the DLQ too.
func (o *Orchestrator) applyFailure(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stageRow *sqlc.JobStage, res *codev1.StageResult, fatal bool, dlq **queue.DeadLetterRequest) error {
	updated, err := q.FailJobStage(ctx, sqlc.FailJobStageParams{
		ID:           stageRow.ID,
		Status:       stageStatusFor(res.GetStatus()),
		LastError:    marshalError(res.GetError()),
		AttemptError: attemptErrorJSON(stageRow.Attempt, res.GetError()),
		Metrics:      mustMetrics(res.GetMetrics()),
	})
	if err != nil {
		return fmt.Errorf("pipeline: record failure for stage %s: %w", stageName(res.GetStage()), err)
	}
	o.metrics.RecordStageOutcome(stageName(res.GetStage()), stageStatusFor(res.GetStatus()))

	cfg, err := o.runConfigFor(ctx, q, job)
	if err != nil {
		return err
	}
	policy := queue.PolicyFor(res.GetStage(), queue.PolicyOptionsFor(cfg))

	if !fatal && updated.Attempt < policy.MaxAttempts {
		resumeAt := o.cfg.Backoff.ResumeAt(time.Now(), uint32(updated.Attempt)) //nolint:gosec // bounded by MaxAttempts
		o.logger.InfoContext(ctx, "orchestrator: retryable failure, backing off",
			"job_id", job.ID, "stage", stageName(res.GetStage()),
			"attempt", updated.Attempt, "max_attempts", policy.MaxAttempts, "resume_after", resumeAt)
		name := stageName(res.GetStage())
		_, err := o.transition(ctx, q, job, parkState(res.GetStage()), &name, updated.Attempt, &resumeAt, marshalError(res.GetError()))
		return err
	}

	return o.deadLetter(ctx, q, job, updated, res.GetStatus(), res.GetError(), policy, dlq)
}

// deadLetter records a terminal failure and stages the DLQ message the
// caller publishes after commit.
func (o *Orchestrator) deadLetter(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, stageRow *sqlc.JobStage, status codev1.Status, stageErr *codev1.Error, policy queue.StagePolicy, dlq **queue.DeadLetterRequest) error {
	stage, err := queue.StageFromName(stageRow.Stage)
	if err != nil {
		return err
	}
	cfg, err := o.runConfigFor(ctx, q, job)
	if err != nil {
		return err
	}
	inputRef, inputSHA, inputErr := o.stageInput(ctx, q, job, stage)
	if inputErr != nil {
		// A dead letter whose payload_ref cannot be resolved is still
		// worth emitting — the attempt history is the part an operator
		// needs. Recorded rather than silently blanked.
		o.logger.WarnContext(ctx, "orchestrator: dead letter without a resolvable payload_ref",
			"job_id", job.ID, "stage", stageRow.Stage, "error", inputErr)
	}

	*dlq = &queue.DeadLetterRequest{
		Envelope: &codev1.StageEnvelope{
			JobId:          job.ID.String(),
			ConsultationId: job.ConsultationID.String(),
			Stage:          stage,
			Attempt:        uint32(stageRow.Attempt), //nolint:gosec // bounded by MaxAttempts
			IdempotencyKey: stageRow.IdempotencyKey,
			TraceId:        job.TraceID,
			SchemaVersion:  queue.SchemaVersion,
			RunConfigId:    job.RunConfigID.String(),
			PayloadRef:     inputRef,
			EnqueuedAt:     timestamppb.New(stageRow.StartedAt.Time),
			Labels:         o.envelopeLabels(job, cfg),
		},
		Attempts:    decodeAttemptHistory(stageRow.ErrorHistory),
		FinalStatus: status,
	}
	_ = inputSHA

	o.logger.ErrorContext(ctx, "orchestrator: routing to stage.dlq",
		"job_id", job.ID, "stage", stageRow.Stage, "attempt", stageRow.Attempt,
		"max_attempts", policy.MaxAttempts, "final_status", status.String(),
		"error_code", stageErr.GetCode(), "trace_id", job.TraceID)

	name := stageRow.Stage
	_, err = o.transition(ctx, q, job, StateDeadLettered, &name, stageRow.Attempt, nil, marshalError(stageErr))
	return err
}

// indexArtifact records the stage's output in the artifacts table so
// GET /consultations/{id}/result can resolve it without re-deriving keys.
//
// The artifact's identity comes from its key, not from the result message:
// §3.3's layout already encodes consultation, stage, run config and kind,
// so parsing the key is both less to get wrong and a check that the worker
// wrote where the contract says it should.
func (o *Orchestrator) indexArtifact(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, res *codev1.StageResult) error {
	ref := res.GetResultRef()
	if ref == "" {
		return nil
	}
	key, err := storage.ParseArtifactKey(ref)
	if err != nil {
		return err
	}
	if key.ConsultationID != job.ConsultationID || key.RunConfigID != job.RunConfigID {
		return fmt.Errorf("pipeline: result_ref %q belongs to consultation %s / run config %s, not %s / %s",
			ref, key.ConsultationID, key.RunConfigID, job.ConsultationID, job.RunConfigID)
	}

	stat := storage.ArtifactStat{ContentType: "application/json"}
	if o.storage != nil {
		stat, err = o.storage.StatArtifact(ctx, ref)
		if err != nil {
			return err
		}
	}
	if _, err := q.UpsertArtifact(ctx, sqlc.UpsertArtifactParams{
		ConsultationID: job.ConsultationID,
		Stage:          key.Stage,
		RunConfigID:    job.RunConfigID,
		Kind:           key.Kind,
		Uri:            ref,
		Sha256:         stat.SHA256,
		Bytes:          stat.Bytes,
		ContentType:    stat.ContentType,
	}); err != nil {
		return fmt.Errorf("pipeline: index artifact %q: %w", ref, err)
	}
	return nil
}

// handleProgressMessage lands one heartbeat. Progress is best-effort by
// design: a dropped heartbeat costs a stale percentage in the UI, never
// pipeline correctness, so a failure here still acknowledges rather than
// filling the PEL with retries of a number that is already out of date.
func (o *Orchestrator) handleProgressMessage(ctx context.Context, msg queue.Message) error {
	hb, err := queue.DecodeHeartbeat(msg)
	if err != nil {
		o.logger.WarnContext(ctx, "orchestrator: undecodable heartbeat", "message_id", msg.ID, "error", err)
		return nil
	}
	jobID, err := uuid.Parse(hb.GetJobId())
	if err != nil {
		return nil
	}
	n, err := o.queries.RecordJobStageHeartbeat(ctx, sqlc.RecordJobStageHeartbeatParams{
		JobID:           jobID,
		Stage:           stageName(hb.GetStage()),
		PercentComplete: clampPercent(hb.GetPercentComplete()),
		Step:            pgTextOrNull(hb.GetStep()),
	})
	if err != nil {
		o.logger.WarnContext(ctx, "orchestrator: recording heartbeat failed", "job_id", jobID, "error", err)
		return nil
	}
	if n == 0 {
		// No running row matched — the stage finished, or the heartbeat
		// arrived out of order. Both are normal on separate streams.
		o.logger.DebugContext(ctx, "orchestrator: heartbeat for a stage that is not running", "job_id", jobID, "stage", stageName(hb.GetStage()))
	}
	return nil
}

func marshalMetrics(m *codev1.StageMetrics) ([]byte, error) {
	if m == nil {
		return []byte(`{}`), nil
	}
	b, err := protojson.MarshalOptions{}.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("pipeline: marshal stage metrics: %w", err)
	}
	return b, nil
}

func mustMetrics(m *codev1.StageMetrics) []byte {
	b, err := marshalMetrics(m)
	if err != nil {
		return []byte(`{}`)
	}
	return b
}

func marshalError(e *codev1.Error) []byte {
	if e == nil {
		return nil
	}
	b, err := protojson.MarshalOptions{}.Marshal(e)
	if err != nil {
		return nil
	}
	return b
}

// attemptErrorJSON renders one AttemptError as the single-element array
// FailJobStage appends to error_history.
func attemptErrorJSON(attempt int32, e *codev1.Error) []byte {
	entry := map[string]any{
		"attempt":     attempt,
		"occurred_at": time.Now().UTC().Format(time.RFC3339Nano),
	}
	if e != nil {
		entry["error"] = map[string]any{
			"code": e.GetCode(), "message": e.GetMessage(),
			"retryable": e.GetRetryable(), "provider_status": e.GetProviderStatus(),
		}
	}
	b, err := json.Marshal([]any{entry})
	if err != nil {
		return []byte(`[]`)
	}
	return b
}

// decodeAttemptHistory turns job_stages.error_history back into the
// DeadLetter.attempts[] field — every attempt's error, from Postgres rather
// than from a stream that may have been trimmed (§2.5).
func decodeAttemptHistory(raw []byte) []*codev1.AttemptError {
	if len(raw) == 0 {
		return nil
	}
	var entries []struct {
		Attempt    uint32 `json:"attempt"`
		OccurredAt string `json:"occurred_at"`
		Error      struct {
			Code           string `json:"code"`
			Message        string `json:"message"`
			Retryable      bool   `json:"retryable"`
			ProviderStatus string `json:"provider_status"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &entries); err != nil {
		return nil
	}
	out := make([]*codev1.AttemptError, 0, len(entries))
	for _, e := range entries {
		ae := &codev1.AttemptError{
			Attempt: e.Attempt,
			Error: &codev1.Error{
				Code: e.Error.Code, Message: e.Error.Message,
				Retryable: e.Error.Retryable, ProviderStatus: e.Error.ProviderStatus,
			},
		}
		if t, err := time.Parse(time.RFC3339Nano, e.OccurredAt); err == nil {
			ae.OccurredAt = timestamppb.New(t)
		}
		out = append(out, ae)
	}
	return out
}

func runningStateFor(stage codev1.Stage) State {
	st, ok := stepFor(stage)
	if !ok {
		return ""
	}
	return st.Running
}

func clampPercent(p float32) float32 {
	switch {
	case p < 0:
		return 0
	case p > 100:
		return 100
	default:
		return p
	}
}
