package queue

import (
	"context"
	"log/slog"

	"github.com/google/uuid"
)

// JobRequest is the durable-record view of a pipeline run go-api hands off
// to go-orchestrator: enough to look the rest up from Postgres
// (job_stages, run_configs), never a payload copy (docs/architecture.md
// §3.2 — artifacts and job state live in Postgres/MinIO, not in the queue
// message).
type JobRequest struct {
	JobID          uuid.UUID
	ConsultationID uuid.UUID
	RunConfigID    uuid.UUID
}

// Enqueuer signals that a job is ready for the orchestrator to pick up.
// Implementations must not block the caller on pipeline work — Enqueue
// returns once the signal is durably sent (or, for NoopEnqueuer, logged),
// not once processing starts. This is what lets
// POST /consultations/{id}/jobs return 202 immediately.
type Enqueuer interface {
	Enqueue(ctx context.Context, req JobRequest) error
}

// NoopEnqueuer keeps go-api runnable with no Redis at all — the posture
// tests and the Phase 1 vertical slice want. The jobs row created by the
// caller is the real durable state; this only logs that a job is waiting
// for a worker that isn't running, so that is visible in go-api's own logs
// rather than jobs silently going nowhere.
type NoopEnqueuer struct {
	Logger *slog.Logger
}

func (e NoopEnqueuer) Enqueue(ctx context.Context, req JobRequest) error {
	if e.Logger != nil {
		e.Logger.WarnContext(ctx, "queue: no orchestrator consumer configured — job created but not dispatched",
			"job_id", req.JobID, "consultation_id", req.ConsultationID, "run_config_id", req.RunConfigID)
	}
	return nil
}

// StreamEnqueuer is the production Enqueuer: it rings go-orchestrator's
// doorbell on job.submitted so a submitted job starts immediately instead
// of waiting for the next dispatch scan.
//
// It publishes identifiers only, and it dispatches no stage — go-api
// remains prohibited from XADDing to any stage.* stream or consuming any
// stream at all (docs/architecture.md §1.2). The orchestrator owns
// dispatch; this only tells it there is something to look at.
//
// Enqueue failing is explicitly not fatal to job submission. The jobs row
// is durable and ListDispatchableJobs finds it on the orchestrator's next
// scan regardless (claude_context.md decision #34), so a Redis blip costs
// dispatch latency, not a lost consultation — which is why the handler
// logs an Enqueue error and still returns 202.
type StreamEnqueuer struct {
	Client *Client
	Logger *slog.Logger
}

func (e StreamEnqueuer) Enqueue(ctx context.Context, req JobRequest) error {
	id, err := e.Client.PublishJobSubmitted(ctx, req)
	if err != nil {
		return err
	}
	if e.Logger != nil {
		e.Logger.InfoContext(ctx, "queue: job submitted",
			"job_id", req.JobID, "consultation_id", req.ConsultationID,
			"run_config_id", req.RunConfigID, "message_id", id)
	}
	return nil
}
