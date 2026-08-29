-- name: CreateJob :one
INSERT INTO jobs (consultation_id, run_config_id, trace_id, eval_run_id)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetJob :one
-- Query pattern: fetch a job with its stages — step 1 of 2 (paired with
-- ListJobStagesByJob). Two round trips, not a JSON-aggregating join: sqlc
-- doesn't need cleverness here and a job's stage count is tiny (asr,
-- redact, nlp, export).
SELECT * FROM jobs WHERE id = $1;

-- name: ListJobsByConsultation :many
SELECT * FROM jobs WHERE consultation_id = $1 ORDER BY created_at;

-- name: ListJobsAwaitingResume :many
-- Orchestrator resume-scan: quota-parked or retry-backoff jobs whose
-- resume_after has elapsed (§2.5 — quota parking is not a retry).
SELECT * FROM jobs
WHERE state = $1 AND resume_after IS NOT NULL AND resume_after <= now()
ORDER BY resume_after;

-- name: UpdateJobState :one
UPDATE jobs
SET state = $2, current_stage = $3, resume_after = $4, error = $5
WHERE id = $1
RETURNING *;

-- name: IncrementJobAttempt :one
UPDATE jobs SET attempt = attempt + 1 WHERE id = $1 RETURNING *;

-- name: LockJob :one
-- Orchestrator transition step 1 (docs/architecture.md §4.1: "all
-- transitions append to audit_log inside the same transaction that
-- performs the transition"). Row-locks the job for the duration of the
-- transaction so two orchestrator replicas — or a redelivered result
-- racing a reaper re-dispatch — cannot interleave a read-modify-write on
-- the same job.
SELECT * FROM jobs WHERE id = $1 FOR UPDATE;

-- name: TransitionJob :one
-- The single write every state transition goes through. state,
-- current_stage, attempt, resume_after and error move together because
-- §2.5's rules relate them: a retry bumps attempt, a quota park sets
-- resume_after and leaves attempt alone, and a terminal failure sets
-- error. Splitting them across statements would let a crash land the job
-- in a combination the state machine never intends.
UPDATE jobs
SET state = $2, current_stage = $3, attempt = $4, resume_after = $5, error = $6
WHERE id = $1
RETURNING *;

-- name: ListDispatchableJobs :many
-- The orchestrator's dispatch scan, and the reason a crashed orchestrator
-- resumes on restart without anyone re-submitting anything: a job parked
-- in a *_queued state is picked up here whether it got there from
-- POST /jobs, a retry backoff, a quota park, or an orchestrator that died
-- between persisting the transition and XADDing the envelope.
--
-- Consent is re-checked at every dispatch, not only at upload (§7.2), so
-- a mid-pipeline revocation halts processing; the same join drops
-- cancel-requested and erased consultations.
SELECT j.*
FROM jobs j
JOIN consultations c ON c.id = j.consultation_id
JOIN consent_records cr ON cr.id = c.consent_record_id
WHERE j.state = ANY(@states::text[])
  AND (j.resume_after IS NULL OR j.resume_after <= now())
  AND c.cancel_requested = false
  AND c.erased_at IS NULL
  AND cr.revoked_at IS NULL
ORDER BY j.updated_at
LIMIT $1;

-- name: ListHaltedJobs :many
-- Every reason a job must stop, in one scan.
--
-- Cancellation is cooperative (§4.4): go-api flips
-- consultations.cancel_requested, and the orchestrator observes it here.
-- Erasure and consent revocation are folded in deliberately: those two
-- conditions also *exclude* a job from ListDispatchableJobs, so if this
-- scan only looked at cancel_requested, a consultation whose consent was
-- withdrawn mid-pipeline would silently stop being dispatched and then sit
-- in a non-terminal state forever — invisible to every operator query that
-- asks "what is still running". §7.2 requires processing to halt, which
-- means reaching a terminal state, not merely ceasing to advance.
--
-- Non-terminal jobs only — a halt arriving after EXPORTED is a no-op, not a
-- state regression.
SELECT j.*
FROM jobs j
JOIN consultations c ON c.id = j.consultation_id
JOIN consent_records cr ON cr.id = c.consent_record_id
WHERE (c.cancel_requested = true OR c.erased_at IS NOT NULL OR cr.revoked_at IS NOT NULL)
  AND NOT (j.state = ANY(@terminal_states::text[]))
ORDER BY j.updated_at
LIMIT $1;

-- name: ListStaleJobs :many
-- Stale-job reaping (the Asynq periodic job): a job sitting in a
-- *_running state with nothing having touched it for longer than the
-- stage's visibility timeout. Distinct from ListStalledJobStages, which
-- finds the stalled *message*; this finds a job whose stage row went
-- missing or whose dispatch never landed at all.
SELECT * FROM jobs
WHERE state = ANY(@states::text[])
  AND updated_at < now() - make_interval(secs => @stale_seconds::double precision)
ORDER BY updated_at
LIMIT $1;

-- name: CountJobsByState :many
-- Feeds the orchestrator's Prometheus gauges (§8: "attempt counts, DLQ
-- depth, quota-park events").
SELECT state, count(*)::bigint AS count FROM jobs GROUP BY state;
