-- name: CreateRunConfig :one
-- Configs are interned on content_hash (ADR-0012): a config that already
-- exists returns the existing row instead of duplicating it.
INSERT INTO run_configs (content_hash, arm, config, schema_version)
VALUES ($1, $2, $3, $4)
ON CONFLICT (content_hash) DO UPDATE SET content_hash = EXCLUDED.content_hash
RETURNING *;

-- name: GetRunConfig :one
SELECT * FROM run_configs WHERE id = $1;

-- name: GetRunConfigByContentHash :one
SELECT * FROM run_configs WHERE content_hash = $1;
