-- name: CreateAuditLogEntry :one
INSERT INTO audit_log (org_id, actor_user_id, actor_service, action, resource_type, resource_id, before, after, trace_id, ip, outcome)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING *;

-- name: ListAuditLogByResource :many
SELECT * FROM audit_log WHERE resource_type = $1 AND resource_id = $2 ORDER BY at DESC;

-- name: ListAuditLogByOrg :many
SELECT * FROM audit_log WHERE org_id = $1 ORDER BY at DESC LIMIT $2 OFFSET $3;
