-- name: CreateEvalRun :one
INSERT INTO eval_runs (name, dataset_split, arm, run_config_id)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetEvalRun :one
SELECT * FROM eval_runs WHERE id = $1;

-- name: UpdateEvalRunStatus :one
UPDATE eval_runs
SET status = $2,
    started_at = CASE WHEN $2 = 'running' AND started_at IS NULL THEN now() ELSE started_at END,
    finished_at = CASE WHEN $2 IN ('completed', 'failed') THEN now() ELSE finished_at END
WHERE id = $1
RETURNING *;
