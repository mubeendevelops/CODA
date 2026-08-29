package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/encoding/protojson"

	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

// ArtifactStatter reads an object's metadata so a stage result's
// result_ref can be indexed into the artifacts table. It is an interface
// (not *storage.Client) so the orchestrator's tests exercise the indexing
// logic without a MinIO container, and so an orchestrator configured
// without object storage degrades to "job_stages.result_ref is the record"
// rather than refusing to run.
type ArtifactStatter interface {
	StatArtifact(ctx context.Context, key string) (storage.ArtifactStat, error)
}

// Config parameterises an Orchestrator. Everything with a sensible default
// has one; the three dependencies do not.
type Config struct {
	// ConsumerName identifies this orchestrator within the `orchestrator`
	// consumer group. It must be stable across restarts of the same
	// process so its own PEL entries are recognisable on recovery, and
	// distinct across replicas.
	ConsumerName string

	// DispatchInterval is the backstop scan cadence. The job.submitted
	// doorbell makes normal submission immediate; this scan is what
	// actually delivers crash recovery, quota-park resumption, and retry
	// wake-ups, so it runs regardless of whether any doorbell rings.
	DispatchInterval time.Duration
	// ReaperInterval is §2.4's 30s stalled-message sweep.
	ReaperInterval time.Duration
	// CancelInterval is how often cancel_requested is polled.
	CancelInterval time.Duration
	// DispatchBatch bounds one scan.
	DispatchBatch int32

	Backoff queue.Backoff

	// ReclaimMinIdle overrides the reaper's XAUTOCLAIM window on the
	// *worker* streams. Zero — the production value — means the per-stage
	// visibility timeout from §4.2.
	//
	// Leave it zero in a deployment. Reclaiming a worker stream earlier
	// than §4.2 allows steals messages from workers that are merely slow
	// and re-runs a ~33K-token GoT stage already in progress. It exists so
	// the integration suite can exercise the real XAUTOCLAIM path without
	// sleeping out a 40-minute window.
	ReclaimMinIdle time.Duration

	// ControlReclaimMinIdle overrides the same window on the
	// orchestrator's *own* streams (results, progress, submissions).
	// Zero means controlStreamReclaimIdle.
	//
	// It is a separate knob from ReclaimMinIdle because the two windows
	// trade off in opposite directions — see controlStreamReclaimIdle. A
	// single knob would force a test that wants fast crash recovery on
	// stage.results to also make the reaper steal healthy worker messages.
	ControlReclaimMinIdle time.Duration
}

func (c *Config) applyDefaults() {
	if c.ConsumerName == "" {
		c.ConsumerName = "orchestrator-" + uuid.NewString()[:8]
	}
	if c.DispatchInterval <= 0 {
		c.DispatchInterval = 5 * time.Second
	}
	if c.ReaperInterval <= 0 {
		c.ReaperInterval = queue.ReaperInterval
	}
	if c.CancelInterval <= 0 {
		c.CancelInterval = 5 * time.Second
	}
	if c.DispatchBatch <= 0 {
		c.DispatchBatch = 32
	}
	if c.Backoff.Base <= 0 {
		c.Backoff = queue.DefaultBackoff
	}
}

// Orchestrator is go-orchestrator's pipeline state machine (ADR-0006,
// docs/architecture.md §4). It owns jobs and job_stages exclusively; no
// other component writes them.
//
// It is stateless between messages. Everything that must survive a restart
// is in Postgres, which is what makes "kill it mid-pipeline and start it
// again" a supported operation rather than a recovery procedure.
type Orchestrator struct {
	pool    *pgxpool.Pool
	queries *sqlc.Queries
	q       *queue.Client
	storage ArtifactStatter
	logger  *slog.Logger
	cfg     Config

	metrics *Metrics
}

// New constructs an Orchestrator. artifacts may be nil, in which case stage
// results are still fully recorded on job_stages but not indexed into the
// artifacts table.
func New(pool *pgxpool.Pool, q *queue.Client, artifacts ArtifactStatter, logger *slog.Logger, cfg Config) *Orchestrator {
	cfg.applyDefaults()
	if logger == nil {
		logger = slog.Default()
	}
	return &Orchestrator{
		pool: pool, queries: sqlc.New(pool), q: q,
		storage: artifacts, logger: logger, cfg: cfg,
		metrics: NewMetrics(),
	}
}

// Metrics exposes the Prometheus collectors for registration.
func (o *Orchestrator) Metrics() *Metrics { return o.metrics }

// Run starts every loop and blocks until ctx is cancelled.
//
// Five concurrent concerns, deliberately separate goroutines rather than
// one select loop: a slow result batch must not delay heartbeat recording,
// and a wedged reaper must not stop dispatch.
func (o *Orchestrator) Run(ctx context.Context) error {
	if err := o.q.EnsureGroups(ctx); err != nil {
		return err
	}

	// Recover before serving. A restarted orchestrator's first duty is to
	// re-dispatch whatever its predecessor left in flight; doing it here
	// rather than waiting for the first ticker means a crash costs
	// milliseconds of downtime, not a full dispatch interval.
	if err := o.DispatchOnce(ctx); err != nil {
		o.logger.WarnContext(ctx, "orchestrator: startup dispatch scan failed", "error", err)
	}

	errCh := make(chan error, 5)
	go func() { errCh <- o.runResults(ctx) }()
	go func() { errCh <- o.runProgress(ctx) }()
	go func() { errCh <- o.runSubmissions(ctx) }()
	go func() { errCh <- o.runDispatchLoop(ctx) }()
	go func() { errCh <- o.runMaintenanceLoop(ctx) }()

	<-ctx.Done()
	// Drain so a loop returning an error after cancellation is logged
	// rather than leaking a goroutine blocked on an unread channel.
	for i := 0; i < 5; i++ {
		if err := <-errCh; err != nil && ctx.Err() == nil {
			o.logger.Error("orchestrator: loop exited with error", "error", err)
		}
	}
	return nil
}

// runResults consumes stage.results. This is the only consumer of that
// stream, and its handler is where at-least-once delivery is turned into
// effectively-once outcomes.
func (o *Orchestrator) runResults(ctx context.Context) error {
	c := &queue.Consumer{
		Client: o.q, Stream: queue.StreamResults, Group: queue.GroupOrchestrator,
		Name: o.cfg.ConsumerName, Logger: o.logger,
		MinIdle: o.controlStreamMinIdle(), ReclaimEvery: o.cfg.ReaperInterval,
	}
	return c.Run(ctx, o.handleResultMessage)
}

// controlStreamReclaimIdle is how long an entry may sit unacknowledged in
// the orchestrator's own consumer group before another orchestrator — or
// this one after a restart — reclaims it.
//
// It is far shorter than any worker stream's visibility timeout, and the
// asymmetry is deliberate. Reclaiming a *worker* stream early re-runs
// expensive in-progress work. Reclaiming a *result* early costs nothing:
// ApplyResult is convergent, so re-handling a result that another consumer
// is also handling reaches the same state. The tradeoff therefore inverts —
// here recovery latency is what matters, because an orchestrator that
// crashed with a result in its PEL has stalled that consultation until
// someone picks it up.
const controlStreamReclaimIdle = 20 * time.Second

func (o *Orchestrator) controlStreamMinIdle() time.Duration {
	if o.cfg.ControlReclaimMinIdle > 0 {
		return o.cfg.ControlReclaimMinIdle
	}
	return controlStreamReclaimIdle
}

// runProgress consumes stage.progress and lands each heartbeat on
// job_stages, which is how progress reaches go-api's SSE endpoint without
// go-api ever touching a stream (§1.2, claude_context.md decision #35).
func (o *Orchestrator) runProgress(ctx context.Context) error {
	c := &queue.Consumer{
		Client: o.q, Stream: queue.StreamProgress, Group: queue.GroupOrchestrator,
		Name: o.cfg.ConsumerName, Logger: o.logger,
		MinIdle: o.controlStreamMinIdle(), ReclaimEvery: o.cfg.ReaperInterval,
	}
	return c.Run(ctx, o.handleProgressMessage)
}

// runSubmissions consumes the job.submitted doorbell. Handling is
// deliberately trivial — dispatch that one job — because the doorbell is
// an optimisation and the dispatch scan is the guarantee.
func (o *Orchestrator) runSubmissions(ctx context.Context) error {
	c := &queue.Consumer{
		Client: o.q, Stream: queue.StreamJobSubmitted, Group: queue.GroupOrchestrator,
		Name: o.cfg.ConsumerName, Logger: o.logger,
		MinIdle: o.controlStreamMinIdle(), ReclaimEvery: o.cfg.ReaperInterval,
	}
	return c.Run(ctx, func(ctx context.Context, msg queue.Message) error {
		jobID, err := uuid.Parse(msg.Field(queue.FieldJobID))
		if err != nil {
			// A malformed doorbell is not worth retrying — ack it and let
			// the scan find the job by state.
			o.logger.WarnContext(ctx, "orchestrator: unparseable job.submitted message", "message_id", msg.ID, "error", err)
			return nil
		}
		if err := o.dispatchJob(ctx, jobID); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: dispatch on submission failed", "job_id", jobID, "error", err)
		}
		// Acked regardless: the scan is the durable path, so a failed
		// doorbell must not accumulate in the PEL forever.
		return nil
	})
}

func (o *Orchestrator) runDispatchLoop(ctx context.Context) error {
	t := time.NewTicker(o.cfg.DispatchInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			if err := o.DispatchOnce(ctx); err != nil && ctx.Err() == nil {
				o.logger.ErrorContext(ctx, "orchestrator: dispatch scan failed", "error", err)
			}
		}
	}
}

func (o *Orchestrator) runMaintenanceLoop(ctx context.Context) error {
	reap := time.NewTicker(o.cfg.ReaperInterval)
	defer reap.Stop()
	cancel := time.NewTicker(o.cfg.CancelInterval)
	defer cancel.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-reap.C:
			if err := o.ReapOnce(ctx); err != nil && ctx.Err() == nil {
				o.logger.ErrorContext(ctx, "orchestrator: reaper sweep failed", "error", err)
			}
		case <-cancel.C:
			if err := o.CancelOnce(ctx); err != nil && ctx.Err() == nil {
				o.logger.ErrorContext(ctx, "orchestrator: cancellation sweep failed", "error", err)
			}
		}
	}
}

// withTx runs fn inside a transaction, rolling back on error.
//
// Every state transition goes through here because §4.1 requires the
// transition and its audit_log row to commit together. That closes, for
// pipeline transitions specifically, the gap §7.4 documents for go-api's
// request handlers — here the orchestrator already owns a multi-statement
// write, so there is a transaction to enlist the audit write in.
func (o *Orchestrator) withTx(ctx context.Context, fn func(ctx context.Context, q *sqlc.Queries) error) error {
	tx, err := o.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("pipeline: begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }() // no-op after a successful commit
	if err := fn(ctx, o.queries.WithTx(tx)); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("pipeline: commit: %w", err)
	}
	return nil
}

// transition writes one job state change plus its audit_log row, in the
// caller's transaction.
func (o *Orchestrator) transition(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, to State, stage *string, attempt int32, resumeAfter *time.Time, jobErr []byte) (*sqlc.Job, error) {
	updated, err := q.TransitionJob(ctx, sqlc.TransitionJobParams{
		ID:           job.ID,
		State:        string(to),
		CurrentStage: stage,
		Attempt:      attempt,
		ResumeAfter:  timestamptzOrNull(resumeAfter),
		Error:        jobErr,
	})
	if err != nil {
		return nil, fmt.Errorf("pipeline: transition job %s to %s: %w", job.ID, to, err)
	}

	consultation, err := q.GetConsultation(ctx, job.ConsultationID)
	if err != nil {
		return nil, fmt.Errorf("pipeline: load consultation %s: %w", job.ConsultationID, err)
	}

	// consultations.state mirrors "whichever job feeds the doctor-facing
	// review UI" (claude_context.md, jobs-is-per-arm note), which is the
	// newest job — the same one GET /consultations/{id}/result reads. An
	// older arm still finishing in the background must not drag the
	// consultation's visible state backwards, so it is checked rather
	// than assumed.
	newest, err := o.isNewestJob(ctx, q, job)
	if err != nil {
		return nil, err
	}
	if newest && consultation.State != string(to) {
		if _, err := q.UpdateConsultationState(ctx, sqlc.UpdateConsultationStateParams{ID: consultation.ID, State: string(to)}); err != nil {
			return nil, fmt.Errorf("pipeline: mirror state onto consultation %s: %w", consultation.ID, err)
		}
	}

	after, _ := json.Marshal(map[string]any{
		"state": string(to), "current_stage": stage, "attempt": attempt,
	})
	before, _ := json.Marshal(map[string]any{
		"state": job.State, "current_stage": job.CurrentStage, "attempt": job.Attempt,
	})
	if _, err := q.CreateAuditLogEntry(ctx, sqlc.CreateAuditLogEntryParams{
		OrgID:        consultation.OrgID,
		ActorService: pgTextOrNull("go-orchestrator"),
		Action:       "job.transition",
		ResourceType: "job",
		ResourceID:   job.ID.String(),
		Before:       before,
		After:        after,
		TraceID:      job.TraceID,
		Outcome:      "success",
	}); err != nil {
		return nil, fmt.Errorf("pipeline: audit transition of job %s: %w", job.ID, err)
	}

	o.metrics.RecordTransition(string(to))
	o.logger.InfoContext(ctx, "orchestrator: job transition",
		"job_id", job.ID, "from", job.State, "to", string(to),
		"stage", derefString(stage), "attempt", attempt, "trace_id", job.TraceID)
	return updated, nil
}

func (o *Orchestrator) isNewestJob(ctx context.Context, q *sqlc.Queries, job *sqlc.Job) (bool, error) {
	jobs, err := q.ListJobsByConsultation(ctx, job.ConsultationID)
	if err != nil {
		return false, fmt.Errorf("pipeline: list jobs for consultation %s: %w", job.ConsultationID, err)
	}
	if len(jobs) == 0 {
		return false, nil
	}
	return jobs[len(jobs)-1].ID == job.ID, nil
}

// runConfigFor loads and decodes a job's RunConfig, which supplies the
// per-stage timeout and max-attempt policy (§4.2) and the models pinned for
// the arm. Timeouts come from the persisted config rather than from the
// environment so a reported result's operational parameters are
// reconstructible from a Postgres dump alone (ADR-0012).
func (o *Orchestrator) runConfigFor(ctx context.Context, q *sqlc.Queries, job *sqlc.Job) (*codev1.RunConfig, error) {
	rc, err := q.GetRunConfig(ctx, job.RunConfigID)
	if err != nil {
		return nil, fmt.Errorf("pipeline: load run config %s: %w", job.RunConfigID, err)
	}
	cfg := &codev1.RunConfig{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(rc.Config, cfg); err != nil {
		return nil, fmt.Errorf("pipeline: decode run config %s: %w", job.RunConfigID, err)
	}
	return cfg, nil
}

// loadJobForUpdate row-locks a job inside the caller's transaction.
func loadJobForUpdate(ctx context.Context, q *sqlc.Queries, jobID uuid.UUID) (*sqlc.Job, error) {
	job, err := q.LockJob(ctx, jobID)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, fmt.Errorf("pipeline: job %s does not exist: %w", jobID, err)
		}
		return nil, fmt.Errorf("pipeline: lock job %s: %w", jobID, err)
	}
	return job, nil
}
