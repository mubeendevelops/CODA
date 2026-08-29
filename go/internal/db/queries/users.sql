-- name: CreateUser :one
INSERT INTO users (org_id, email, password_hash, role)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetUser :one
SELECT * FROM users WHERE id = $1;

-- name: GetUserByEmail :one
SELECT * FROM users WHERE email = $1;

-- name: ListUsersByOrg :many
SELECT * FROM users WHERE org_id = $1 ORDER BY created_at;
