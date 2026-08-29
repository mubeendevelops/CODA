-- name: ClaimJobStage :one
-- Dedupe protocol step 2 (§2.3): claim the row before doing any work.
-- ON CONFLICT DO NOTHING means a retry/redelivery for the same
-- idempotency_key claims nothing new — the caller falls through to
-- GetJobStageByIdempotencyKey to find the existing row's status.
INSERT INTO job_stages (job_id, stage, idempotency_key, status, attempt)
VALUES ($1, $2, $3, 'running', 1)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *;

-- name: GetJobStageByIdempotencyKey :one
-- Dedupe protocol step 1 (§2.3): check for an already-succeeded row before
-- doing any work.
SELECT * FROM job_stages WHERE idempotency_key = $1;

-- name: ListJobStagesByJob :many
-- Query pattern: fetch a job with its stages — step 2 of 2.
SELECT * FROM job_stages WHERE job_id = $1 ORDER BY started_at NULLS LAST;

-- name: UpdateJobStageResult :one
UPDATE job_stages
SET status = $2, result_ref = $3, metrics = $4, finished_at = now()
WHERE id = $1
RETURNING *;

-- name: IncrementJobStageAttempt :one
UPDATE job_stages
SET attempt = attempt + 1, status = 'running', started_at = COALESCE(started_at, now())
WHERE id = $1
RETURNING *;
