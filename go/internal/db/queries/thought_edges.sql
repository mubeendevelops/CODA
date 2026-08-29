-- name: CreateThoughtEdge :one
INSERT INTO thought_edges (consultation_id, run_config_id, src_thought_id, dst_thought_id, edge_type, weight, predicted_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: ListThoughtEdgesByConsultationAndRunConfig :many
-- Query pattern: fetch full pipeline artifacts for one consultation — the
-- serialized thought graph (nodes from ListThoughtsByConsultationAndRunConfig,
-- edges from here) for the case-study figures (plan.md Phase 6/10).
SELECT * FROM thought_edges WHERE consultation_id = $1 AND run_config_id = $2;
