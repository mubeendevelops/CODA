-- name: CreateThought :one
INSERT INTO thoughts (consultation_id, run_config_id, turn_id, speaker, text, entities, category, temporal_anchor, linked_concepts)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING *;

-- name: ListThoughtsByConsultationAndRunConfig :many
-- Query pattern: fetch full pipeline artifacts for one consultation.
SELECT * FROM thoughts WHERE consultation_id = $1 AND run_config_id = $2 ORDER BY created_at;
