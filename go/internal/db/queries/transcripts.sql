-- name: CreateTranscript :one
INSERT INTO transcripts (consultation_id, run_config_id, uri, asr_backend, asr_model, wer, der, language)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: GetTranscriptForConsultationAndRunConfig :one
SELECT * FROM transcripts WHERE consultation_id = $1 AND run_config_id = $2;
