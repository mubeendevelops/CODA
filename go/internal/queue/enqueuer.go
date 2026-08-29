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

// NoopEnqueuer is the stub implementation for before go-orchestrator and
// its Redis Streams consumer exist (plan.md Phase 3). The jobs row created
// by the caller is the real durable state; this only logs that a job is
// waiting for a worker that isn't running yet, so that's visible in
// go-api's own logs during Phase 1/3 development rather than jobs silently
// going nowhere.
type NoopEnqueuer struct {
	Logger *slog.Logger
}

func (e NoopEnqueuer) Enqueue(ctx context.Context, req JobRequest) error {
	if e.Logger != nil {
		e.Logger.WarnContext(ctx, "queue: no orchestrator consumer yet — job created but not dispatched",
			"job_id", req.JobID, "consultation_id", req.ConsultationID, "run_config_id", req.RunConfigID)
	}
	return nil
}
