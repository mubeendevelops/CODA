-- name: CreateClinicalNote :one
INSERT INTO clinical_notes (consultation_id, run_config_id, version, status, note)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetClinicalNoteForConsultationAndRunConfig :one
-- Query pattern: fetch full pipeline artifacts for one consultation.
SELECT * FROM clinical_notes
WHERE consultation_id = $1 AND run_config_id = $2
ORDER BY version DESC
LIMIT 1;

-- name: ApproveClinicalNote :one
-- No auto-save before doctor approval (§7.5) — this is the only sanctioned
-- transition into status='approved', and it is always an explicit,
-- authenticated call from the API layer.
UPDATE clinical_notes
SET status = 'approved', approved_by = $2, approved_at = now()
WHERE id = $1
RETURNING *;
