// Package tasks holds go-orchestrator's Asynq-scheduled periodic jobs.
//
// Why Asynq and not the Streams transport: ADR-0002 keeps Asynq "for
// Go-internal jobs (exports, erasure, reaping) where both ends are Go".
// These three are exactly that — no Python worker participates, no clinical
// payload moves — and Asynq supplies the cron scheduling, retry, and
// dedupe-by-task-ID that hand-rolled tickers would otherwise reimplement
// badly. Streams stay reserved for the cross-language pipeline.
//
// Asynq runs on its own Redis database (config.Redis.AsynqDB), separate
// from the pipeline streams, so its keyspace and theirs can never collide.
package tasks

import (
	"time"

	"github.com/hibiken/asynq"
)

// Task type names. Prefixed so they are distinguishable from any future
// task in a shared Redis.
const (
	TypeDLQAlert          = "coda:dlq_alert"
	TypeReapStaleJobs     = "coda:reap_stale_jobs"
	TypeArtifactRetention = "coda:artifact_retention_sweep"
)

// Schedule is the cron spec for each periodic task, and the queue they run
// on.
type Schedule struct {
	DLQAlert          string
	ReapStaleJobs     string
	ArtifactRetention string
}

// DefaultSchedule reflects what each job is actually for.
//
//   - DLQ alerting every minute: a dead-lettered consultation is a stopped
//     consultation, and on a research timeline the cost of noticing late is
//     a wasted overnight sweep.
//   - Stale-job reaping every 5 minutes: the orchestrator's own 30s reaper
//     (queue.ReaperInterval) is the primary mechanism; this is the backstop
//     for jobs whose stage row never got written at all, which is rare
//     enough that a fast cadence would only add load.
//   - Retention sweep hourly: it lists object storage, so it is the
//     expensive one, and nothing it cleans up is urgent.
var DefaultSchedule = Schedule{
	DLQAlert:          "@every 1m",
	ReapStaleJobs:     "@every 5m",
	ArtifactRetention: "@every 1h",
}

// QueueName is the single Asynq queue these run on. One queue, because
// there are three tasks and none of them can starve the others.
const QueueName = "orchestrator"

// taskOpts are shared across all three. Retention is short because every
// one of these tasks is idempotent and periodic — a failed run is
// superseded by the next scheduled one, so keeping failed task records
// around only grows Redis.
func taskOpts() []asynq.Option {
	return []asynq.Option{
		asynq.Queue(QueueName),
		asynq.MaxRetry(2),
		asynq.Timeout(5 * time.Minute),
		asynq.Retention(1 * time.Hour),
	}
}

// RegisterPeriodic enrols the three periodic jobs on an Asynq scheduler.
func RegisterPeriodic(s *asynq.Scheduler, sched Schedule) error {
	for _, e := range []struct {
		cron string
		typ  string
	}{
		{sched.DLQAlert, TypeDLQAlert},
		{sched.ReapStaleJobs, TypeReapStaleJobs},
		{sched.ArtifactRetention, TypeArtifactRetention},
	} {
		if e.cron == "" {
			continue
		}
		if _, err := s.Register(e.cron, asynq.NewTask(e.typ, nil), taskOpts()...); err != nil {
			return err
		}
	}
	return nil
}
