-- name: CreateReview :one
INSERT INTO reviews (clinical_note_id, reviewer_user_id)
VALUES ($1, $2)
RETURNING *;

-- name: CompleteReview :one
UPDATE reviews SET completed_at = now(), outcome = $2 WHERE id = $1 RETURNING *;

-- name: GetReview :one
SELECT * FROM reviews WHERE id = $1;
