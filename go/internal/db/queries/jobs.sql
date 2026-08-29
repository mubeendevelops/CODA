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
