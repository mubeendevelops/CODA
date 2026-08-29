-- name: GetLLMCacheEntry :one
SELECT * FROM llm_cache WHERE model = $1 AND prompt_sha256 = $2;

-- name: CreateLLMCacheEntry :one
INSERT INTO llm_cache (model, prompt_sha256, response, tokens_in, tokens_out)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (model, prompt_sha256) DO UPDATE SET hit_count = llm_cache.hit_count + 1
RETURNING *;

-- name: IncrementLLMCacheHit :one
UPDATE llm_cache SET hit_count = hit_count + 1 WHERE id = $1 RETURNING *;
