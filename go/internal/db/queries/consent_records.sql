-- name: CreateConsentRecord :one
INSERT INTO consent_records (org_id, subject_ref, consent_type, granted_at, granted_by, artifact_uri)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetConsentRecord :one
SELECT * FROM consent_records WHERE id = $1;

-- name: RevokeConsentRecord :one
UPDATE consent_records SET revoked_at = now() WHERE id = $1 RETURNING *;
