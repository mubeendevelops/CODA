-- name: CreateArtifact :one
INSERT INTO artifacts (consultation_id, stage, run_config_id, kind, uri, sha256, bytes, content_type)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListArtifactsByConsultation :many
-- Query pattern: fetch full pipeline artifacts for one consultation.
-- Pass run_config_id = NULL to fetch across every arm.
SELECT *
FROM artifacts
WHERE consultation_id = $1
  AND (sqlc.narg('run_config_id')::uuid IS NULL OR run_config_id = sqlc.narg('run_config_id'))
ORDER BY created_at;
