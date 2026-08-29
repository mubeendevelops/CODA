-- name: CreateEvalResult :one
INSERT INTO eval_results (eval_run_id, consultation_id, run_config_id, metric_key, metric_value, language, reference_provenance)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: AggregateEvalResultsByRunConfig :many
-- Query pattern: aggregate eval results by run-config. English and
-- Kannada-English are never averaged into one number (claude_context.md
-- §2) — language stays a GROUP BY key, never collapsed.
SELECT
    run_config_id,
    metric_key,
    language,
    COUNT(*)::bigint AS n,
    AVG(metric_value)::double precision AS mean_value,
    MIN(metric_value)::double precision AS min_value,
    MAX(metric_value)::double precision AS max_value
FROM eval_results
WHERE eval_run_id = $1
GROUP BY run_config_id, metric_key, language
ORDER BY run_config_id, metric_key, language;
