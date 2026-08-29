-- name: CreateExtraction :one
INSERT INTO extractions (
    consultation_id, run_config_id, field_key, value, candidate_set_uri,
    selected_candidate_idx, score_breakdown, iteration
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListExtractionsByConsultationAndRunConfig :many
-- Query pattern: fetch full pipeline artifacts for one consultation.
SELECT * FROM extractions WHERE consultation_id = $1 AND run_config_id = $2 ORDER BY field_key, iteration;
