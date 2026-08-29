-- name: CreateConsultation :one
INSERT INTO consultations (
    org_id, owner_user_id, consent_record_id, consent_obtained, consent_method,
    consent_recorded_at, language
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: GetConsultation :one
SELECT * FROM consultations WHERE id = $1;

-- name: ListConsultationsByOrgAndState :many
-- Query pattern: list consultations by org with a status filter.
-- Pass state = NULL to list every state for the org.
SELECT *
FROM consultations
WHERE org_id = $1
  AND (sqlc.narg('state')::text IS NULL OR state = sqlc.narg('state')::text)
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;

-- name: UpdateConsultationState :one
UPDATE consultations SET state = $2 WHERE id = $1 RETURNING *;

-- name: EraseConsultation :one
-- DPDP erasure on consent withdrawal (§7.2): nulls the PII-bearing columns
-- and sets the tombstone timestamp. The row itself is retained.
UPDATE consultations
SET source_audio_uri = NULL,
    audio_sha256 = NULL,
    erased_at = now()
WHERE id = $1
RETURNING *;
