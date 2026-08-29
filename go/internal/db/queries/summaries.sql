-- name: CreateSummary :one
INSERT INTO summaries (consultation_id, run_config_id, text, rouge_l, bertscore)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetSummaryForConsultationAndRunConfig :one
-- Query pattern: fetch full pipeline artifacts for one consultation.
SELECT * FROM summaries WHERE consultation_id = $1 AND run_config_id = $2;
