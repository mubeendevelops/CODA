-- name: CreateReferenceLabel :one
INSERT INTO reference_labels (consultation_id, field_key, value, provenance, generated_by_model)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: MarkReferenceLabelVerified :one
UPDATE reference_labels
SET provenance = 'human_verified', verified_by_user = $2, verified_at = now()
WHERE id = $1
RETURNING *;

-- name: ListReferenceLabelsByConsultation :many
SELECT * FROM reference_labels WHERE consultation_id = $1 ORDER BY field_key;
