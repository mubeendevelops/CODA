package tasks

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"github.com/hibiken/asynq"

	"coda/go/internal/db/sqlc"
	"coda/go/internal/pipeline"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

// ObjectStore is the slice of storage.Client the retention sweep needs.
// Narrow interface so the sweep is testable without MinIO.
type ObjectStore interface {
	ListKeys(ctx context.Context, prefix string) ([]string, error)
	RemoveObject(ctx context.Context, key string) error
	ConsultationPrefix(consultationID uuidLike) string
}

// Handlers owns the dependencies the three periodic jobs need.
type Handlers struct {
	Queries      *sqlc.Queries
	Queue        *queue.Client
	Orchestrator Orchestrator
	Store        ObjectStore
	Logger       *slog.Logger
	Metrics      *pipeline.Metrics

	// DLQAlertThreshold is the depth at or above which the DLQ alert logs
	// at error level rather than info. 1, because a single dead-lettered
	// consultation in a research pipeline is already something a human
	// needs to look at — this is not a high-volume production queue where
	// a nonzero DLQ is background noise.
	DLQAlertThreshold int64

	// RetentionScanLimit bounds one retention sweep's consultation batch.
	RetentionScanLimit int32
}

// Orchestrator is the slice of *pipeline.Orchestrator the reaping task
// uses, as an interface so the task is testable without a full
// orchestrator.
type Orchestrator interface {
	ReapOnce(ctx context.Context) error
	ResumeJob(ctx context.Context, jobID uuidLike) error
}

// Register wires the three handlers onto an Asynq mux.
func (h *Handlers) Register(mux *asynq.ServeMux) {
	mux.HandleFunc(TypeDLQAlert, h.HandleDLQAlert)
	mux.HandleFunc(TypeReapStaleJobs, h.HandleReapStaleJobs)
	mux.HandleFunc(TypeArtifactRetention, h.HandleArtifactRetention)
}

// HandleDLQAlert surfaces the DLQ (docs/architecture.md §8's "DLQ depth"
// series). stage.dlq deliberately has no automatic consumer (§2.1: "manual
// / operator"), so without something watching its depth a poison message is
// invisible until someone thinks to run XLEN — which on an unattended
// multi-day eval sweep means noticing after the sweep is over.
//
// It reads without consuming (XREVRANGE, not XREADGROUP): a DLQ that drains
// itself is indistinguishable from one that works.
//
// It also refreshes the job-state gauges, since it already runs on the
// cadence a metrics scrape wants and both are counts-of-the-world rather
// than events.
func (h *Handlers) HandleDLQAlert(ctx context.Context, _ *asynq.Task) error {
	depth, err := h.Queue.DLQDepth(ctx)
	if err != nil {
		return fmt.Errorf("tasks: read dlq depth: %w", err)
	}
	if h.Metrics != nil {
		h.Metrics.DLQDepth.Set(float64(depth))
	}

	if counts, err := h.Queries.CountJobsByState(ctx); err != nil {
		h.Logger.WarnContext(ctx, "tasks: refreshing job-state gauges failed", "error", err)
	} else if h.Metrics != nil {
		h.Metrics.JobsByState.Reset() // states that dropped to zero must not keep their last value
		for _, c := range counts {
			h.Metrics.JobsByState.WithLabelValues(c.State).Set(float64(c.Count))
		}
	}

	threshold := h.DLQAlertThreshold
	if threshold <= 0 {
		threshold = 1
	}
	if depth < threshold {
		h.Logger.DebugContext(ctx, "tasks: dlq is clear", "depth", depth)
		return nil
	}

	// Log the most recent entries alongside the depth. A bare count tells
	// an operator something is wrong; the stage, job and error code tell
	// them whether it is one broken consultation or a systemic failure —
	// which is the difference between replaying one message and stopping
	// the sweep.
	recent, err := h.Queue.ReadDeadLetters(ctx, 10)
	if err != nil {
		h.Logger.ErrorContext(ctx, "tasks: dead letters present but unreadable", "depth", depth, "error", err)
		return nil
	}
	var summary []string
	for _, dl := range recent {
		last := ""
		if n := len(dl.GetAttempts()); n > 0 {
			last = dl.GetAttempts()[n-1].GetError().GetCode()
		}
		summary = append(summary, fmt.Sprintf("job=%s stage=%s status=%s attempts=%d last_error=%s",
			dl.GetJobId(), queue.StageName(dl.GetStage()), dl.GetFinalStatus().String(), len(dl.GetAttempts()), last))
	}
	h.Logger.ErrorContext(ctx, "tasks: stage.dlq has entries needing operator attention",
		"depth", depth, "recent", strings.Join(summary, " | "))
	return nil
}

// HandleReapStaleJobs is the backstop for the orchestrator's own 30s reaper.
//
// It runs the same sweep (so a wedged in-process reaper goroutine does not
// silently stop recovery), then looks for jobs the sweep structurally
// cannot see: a job sitting in a *_running state with no corresponding
// stalled stage row — which happens if a crash landed between marking the
// job running and writing the stage row, or if the stage row was
// hand-edited. Those are re-derived from their completed stages by
// ResumeJob (§4.3), never re-run from the beginning.
func (h *Handlers) HandleReapStaleJobs(ctx context.Context, _ *asynq.Task) error {
	if h.Orchestrator == nil {
		return nil
	}
	if err := h.Orchestrator.ReapOnce(ctx); err != nil {
		h.Logger.WarnContext(ctx, "tasks: reaper sweep reported an error", "error", err)
	}

	limit := h.RetentionScanLimit
	if limit <= 0 {
		limit = 100
	}
	stale, err := h.Queries.ListStaleJobs(ctx, sqlc.ListStaleJobsParams{
		Limit:  limit,
		States: pipeline.RunningStateNames(),
		// Twice the longest visibility timeout in §4.2 (the 90-minute GoT
		// window). A job untouched for longer than that is stuck by any
		// reading — no heartbeat, no result, no reclaim has moved it —
		// whereas a tighter bound would reap healthy long GoT stages.
		StaleSeconds: (2 * queue.PolicyFor(stageNLP, queue.PolicyOptions{GoTArm: true}).VisibilityTimeout).Seconds(),
	})
	if err != nil {
		return fmt.Errorf("tasks: scan stale jobs: %w", err)
	}
	for _, job := range stale {
		h.Logger.WarnContext(ctx, "tasks: job stuck in a running state past every stage window, resuming it from its last successful stage",
			"job_id", job.ID, "state", job.State, "updated_at", job.UpdatedAt.Time)
		if err := h.Orchestrator.ResumeJob(ctx, job.ID); err != nil {
			h.Logger.ErrorContext(ctx, "tasks: resuming a stale job failed", "job_id", job.ID, "error", err)
		}
	}
	return nil
}

// HandleArtifactRetention reconciles object storage against the artifacts
// table.
//
// Scope, decided deliberately: **orphans only, never age**. The system has
// exactly one retention policy — DPDP erasure on consent withdrawal (§7.2)
// — and claude_context.md records that no table has an independent one. So
// this sweep enforces the policy that exists rather than inventing a new
// one:
//
//  1. A consultation with erased_at set should have no objects left. If
//     erasure crashed part-way through deleting them, this converges.
//  2. An object under a live consultation with no artifacts row is
//     unreferenced — a stage that wrote its output and then failed before
//     the result was persisted. Nothing can ever read it again.
//
// Deleting on age was considered and rejected: an ablation result must stay
// reconstructible from the database plus its artifacts (§6.3), and a
// time-expired candidate_set makes that arm's provenance unreconstructible.
// Bounding disk during a long sweep is a real concern, but it is not worth
// paying for with the reproducibility guarantee the whole project rests on.
func (h *Handlers) HandleArtifactRetention(ctx context.Context, _ *asynq.Task) error {
	if h.Store == nil {
		return nil
	}
	limit := h.RetentionScanLimit
	if limit <= 0 {
		limit = 100
	}

	erased, err := h.Queries.ListErasedConsultations(ctx, limit)
	if err != nil {
		return fmt.Errorf("tasks: list erased consultations: %w", err)
	}
	deleted, orphans := 0, 0
	for _, c := range erased {
		keys, err := h.Store.ListKeys(ctx, h.Store.ConsultationPrefix(c.ID))
		if err != nil {
			h.Logger.WarnContext(ctx, "tasks: listing an erased consultation's objects failed", "consultation_id", c.ID, "error", err)
			continue
		}
		for _, key := range keys {
			if err := h.Store.RemoveObject(ctx, key); err != nil {
				h.Logger.WarnContext(ctx, "tasks: deleting an erased consultation's object failed", "key", key, "error", err)
				continue
			}
			deleted++
		}
		if len(keys) > 0 {
			h.Logger.InfoContext(ctx, "tasks: completed a partially-applied erasure",
				"consultation_id", c.ID, "objects_deleted", len(keys))
		}
	}

	// Orphan pass. Source audio is skipped: it is recorded on the
	// consultations row (decision #36), not in artifacts, so every source
	// object would otherwise look unreferenced and be deleted — which
	// would destroy the one immutable input every ablation arm shares.
	all, err := h.Store.ListKeys(ctx, "")
	if err != nil {
		return fmt.Errorf("tasks: list objects: %w", err)
	}
	for _, key := range all {
		parsed, err := storage.ParseArtifactKey(key)
		if err != nil {
			continue // not a stage artifact — source audio, consent, exports
		}
		if _, err := h.Queries.GetArtifactByURI(ctx, key); err == nil {
			continue // referenced
		}
		h.Logger.InfoContext(ctx, "tasks: deleting an unreferenced stage artifact",
			"key", key, "consultation_id", parsed.ConsultationID, "kind", parsed.Kind)
		if err := h.Store.RemoveObject(ctx, key); err != nil {
			h.Logger.WarnContext(ctx, "tasks: deleting an orphaned object failed", "key", key, "error", err)
			continue
		}
		orphans++
	}

	h.Logger.InfoContext(ctx, "tasks: artifact retention sweep complete",
		"erased_objects_deleted", deleted, "orphans_deleted", orphans, "consultations_scanned", len(erased))
	return nil
}
