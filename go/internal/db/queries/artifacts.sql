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

-- name: UpsertArtifact :one
-- Artifact keys are immutable (§3.3), so a conflict on uri means a
-- *duplicate delivery* of the same stage result, not a changed artifact —
-- at-least-once delivery (ADR-0007) makes that routine. DO UPDATE with a
-- no-op assignment (rather than DO NOTHING) so the existing row is still
-- RETURNINGed and the caller has one code path.
INSERT INTO artifacts (consultation_id, stage, run_config_id, kind, uri, sha256, bytes, content_type)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (uri) DO UPDATE SET uri = EXCLUDED.uri
RETURNING *;

-- name: GetArtifactByURI :one
-- Orphan detection for the retention sweep: a MinIO object with no
-- artifacts row is unreferenced.
SELECT * FROM artifacts WHERE uri = $1;
