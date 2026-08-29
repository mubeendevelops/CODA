-- name: CreateTurn :one
INSERT INTO turns (transcript_id, turn_index, speaker_label, start_ms, end_ms, text, confidence)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: SetTurnRedactedText :one
-- Written by the mandatory REDACT_RUNNING stage (§7.3); only this text is
-- ever placed in a prompt.
UPDATE turns SET text_redacted = $2 WHERE id = $1 RETURNING *;

-- name: ListTurnsByTranscript :many
SELECT * FROM turns WHERE transcript_id = $1 ORDER BY turn_index;
