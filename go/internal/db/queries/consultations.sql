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
-- Query pattern: list/filter/paginate consultations by org, org-scoped.
-- Pass state/language = NULL to not filter on that dimension.
SELECT *
FROM consultations
WHERE org_id = $1
  AND (sqlc.narg('state')::text IS NULL OR state = sqlc.narg('state')::text)
  AND (sqlc.narg('language')::text IS NULL OR language = sqlc.narg('language')::text)
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;

-- name: UpdateConsultationState :one
UPDATE consultations SET state = $2 WHERE id = $1 RETURNING *;

-- name: UpdateConsultationAudio :one
-- Written by the audio-confirm endpoint once StatObject verifies the
-- upload landed — source_audio_uri/audio_sha256/duration_sec are direct
-- columns on consultations (architecture.md §5.2), not an `artifacts` row:
-- the raw source recording isn't run-config-dependent the way stage
-- outputs are, and `artifacts.run_config_id` is NOT NULL.
UPDATE consultations
SET source_audio_uri = $2, audio_sha256 = $3, duration_sec = $4
WHERE id = $1
RETURNING *;

-- name: EraseConsultation :one
-- DPDP erasure on consent withdrawal (§7.2): nulls the PII-bearing columns
-- and sets the tombstone timestamp. The row itself is retained.
UPDATE consultations
SET source_audio_uri = NULL,
    audio_sha256 = NULL,
    erased_at = now()
WHERE id = $1
RETURNING *;

-- name: DeleteConsultation :exec
-- Full cascading deletion (not the EraseConsultation tombstone above) —
-- explicitly requested for DELETE /consultations/{id}: every FK from
-- jobs/artifacts/transcripts/turns/thoughts/thought_edges/extractions/
-- summaries/clinical_notes/reviews/review_edits down to consultations is
-- ON DELETE CASCADE, so this one statement removes the whole subtree.
-- consent_records is untouched (consultations -> consent_records is ON
-- DELETE RESTRICT, the compliance record outlives the consultation).
-- The caller must capture what it needs for the audit `before` snapshot
-- and delete MinIO objects *before* calling this — the row is gone after.
DELETE FROM consultations WHERE id = $1;
