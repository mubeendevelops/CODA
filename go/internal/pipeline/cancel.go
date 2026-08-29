package pipeline

import (
	"context"
	"encoding/json"
	"fmt"

	"coda/go/internal/db/sqlc"
)

// CancelOnce observes every reason a job must stop: cooperative
// cancellation (§4.4), DPDP erasure, and consent revocation (§7.2).
//
// go-api sets consultations.cancel_requested; this is the orchestrator half
// of that protocol. It stops dispatching and moves every non-terminal job of
// the consultation to CANCELLED.
//
// Consent revocation is handled here rather than only in the dispatch scan
// for a reason worth stating: the scan *excludes* revoked-consent jobs, so
// on its own it makes such a job stop advancing without ever making it stop.
// A job frozen mid-pipeline in a non-terminal state satisfies "don't process
// it" but not §7.2's "halt processing" — it is invisible to any operator
// query asking what is still in flight, and it never releases its slot in
// the eval sweep's accounting. This sweep is what actually terminates it.
//
// What it deliberately does not do:
//
//   - interrupt an in-flight provider call. §4.4: "in-flight provider calls
//     are not interrupted — already-spent tokens are still accounted". The
//     worker sees the flag at its own next checkpoint and emits CANCELLED.
//   - delete artifacts from completed stages. They stay valid inputs for a
//     later re-run, so cancelling and re-submitting does not re-pay for the
//     stages that already finished.
func (o *Orchestrator) CancelOnce(ctx context.Context) error {
	jobs, err := o.queries.ListHaltedJobs(ctx, sqlc.ListHaltedJobsParams{
		Limit:          o.cfg.DispatchBatch,
		TerminalStates: TerminalStateNames(),
	})
	if err != nil {
		return fmt.Errorf("pipeline: scan halted jobs: %w", err)
	}
	for _, j := range jobs {
		if err := o.cancelJob(ctx, j.ID); err != nil {
			o.logger.ErrorContext(ctx, "orchestrator: halting job failed", "job_id", j.ID, "error", err)
		}
	}
	return nil
}

// cancelJob re-derives the halt reason under the job's row lock rather than
// trusting the scan's snapshot, so jobs.error records why this job stopped
// — cancelled, erased, or consent-revoked stay distinguishable afterwards.
func (o *Orchestrator) cancelJob(ctx context.Context, jobID uuidLike) error {
	return o.withTx(ctx, func(ctx context.Context, q *sqlc.Queries) error {
		job, err := loadJobForUpdate(ctx, q, jobID)
		if err != nil {
			return err
		}
		if IsTerminal(State(job.State)) {
			return nil
		}
		halted, reason, err := o.consultationHalted(ctx, q, job)
		if err != nil {
			return err
		}
		if !halted {
			// Resolved between the scan and the lock — consent re-granted,
			// or the flag cleared. Nothing to do.
			return nil
		}
		return o.haltJob(ctx, q, job, reason)
	})
}

// haltJob moves a job to CANCELLED and marks any in-flight stage row
// cancelled, in the caller's transaction.
//
// Consent revocation lands here too, not in a state of its own. §7.2 says
// the orchestrator "refuses to dispatch any stage for a consultation whose
// consent is missing or whose revoked_at is set" but §4.1 defines no state
// for that outcome; CANCELLED is the honest fit, since withdrawing consent
// is the data subject exercising a right — an owner cancellation in §4.1's
// own terms — not a system failure. The reason is recorded in jobs.error so
// the two causes stay distinguishable in the database.
func (o *Orchestrator) haltJob(ctx context.Context, q *sqlc.Queries, job *sqlc.Job, reason string) error {
	stages, err := q.ListJobStagesByJob(ctx, job.ID)
	if err != nil {
		return fmt.Errorf("pipeline: list stages of job %s: %w", job.ID, err)
	}
	for _, s := range stages {
		if s.Status != "running" && s.Status != "pending" {
			continue
		}
		if _, err := q.FailJobStage(ctx, sqlc.FailJobStageParams{
			ID:           s.ID,
			Status:       "cancelled",
			LastError:    mustJSON(map[string]any{"code": "CANCELLED", "message": reason}),
			AttemptError: mustJSON([]any{map[string]any{"attempt": s.Attempt, "error": map[string]any{"code": "CANCELLED", "message": reason}}}),
			Metrics:      s.Metrics,
		}); err != nil {
			return fmt.Errorf("pipeline: cancel stage %s of job %s: %w", s.Stage, job.ID, err)
		}
	}

	o.logger.InfoContext(ctx, "orchestrator: halting job", "job_id", job.ID, "reason", reason, "from", job.State)
	o.metrics.RecordCancellation(reason)
	_, err = o.transition(ctx, q, job, StateCancelled, job.CurrentStage, job.Attempt,
		nil, mustJSON(map[string]any{"code": "CANCELLED", "message": reason}))
	return err
}

// consultationHalted re-checks, at every dispatch, the conditions that must
// stop processing: an explicit cancellation, DPDP erasure, or withdrawn
// consent.
//
// The dispatch scan already filters all three in SQL. This is the same
// check again, under the job's row lock, because the scan's snapshot can be
// stale by the time the lock is granted — and dispatching one stage of a
// consultation whose consent was revoked in that window is precisely the
// failure §7.2's "re-evaluated at every stage dispatch" exists to prevent.
func (o *Orchestrator) consultationHalted(ctx context.Context, q *sqlc.Queries, job *sqlc.Job) (bool, string, error) {
	c, err := q.GetConsultation(ctx, job.ConsultationID)
	if err != nil {
		return false, "", fmt.Errorf("pipeline: load consultation %s: %w", job.ConsultationID, err)
	}
	if c.CancelRequested {
		return true, "cancel_requested", nil
	}
	if c.ErasedAt.Valid {
		return true, "consultation_erased", nil
	}
	consent, err := q.GetConsentRecord(ctx, c.ConsentRecordID)
	if err != nil {
		return false, "", fmt.Errorf("pipeline: load consent record %s: %w", c.ConsentRecordID, err)
	}
	if consent.RevokedAt.Valid {
		return true, "consent_revoked", nil
	}
	return false, "", nil
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		return []byte(`{}`)
	}
	return b
}
