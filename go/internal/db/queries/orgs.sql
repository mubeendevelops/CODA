-- name: CreateOrg :one
INSERT INTO orgs (name, region)
VALUES ($1, $2)
RETURNING *;

-- name: GetOrg :one
SELECT * FROM orgs WHERE id = $1;

-- name: GetOrgByName :one
-- No uniqueness constraint on name (an org can rename); used only by the
-- idempotent seed script to find its fixed dev org on a re-run.
SELECT * FROM orgs WHERE name = $1;
