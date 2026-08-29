-- name: CreateReviewEdit :one
-- The HITL data flywheel (decision #14).
INSERT INTO review_edits (review_id, field_key, original_value, edited_value, edit_type, editor_user_id)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: ListReviewEditsByReview :many
SELECT * FROM review_edits WHERE review_id = $1 ORDER BY edited_at;
