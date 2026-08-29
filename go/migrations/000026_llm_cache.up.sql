-- Keyed on (model, prompt_sha256): shared stages are paid for once across
-- arms, and eval re-runs are near-free (ADR-0015). No updated_at trigger —
-- a cache entry is written once; hit_count increments via application UPDATE.
CREATE TABLE llm_cache (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model          text NOT NULL,
    prompt_sha256  text NOT NULL,
    response       jsonb NOT NULL,
    tokens_in      int NOT NULL,
    tokens_out     int NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    hit_count      int NOT NULL DEFAULT 0,
    CONSTRAINT llm_cache_model_prompt_unique UNIQUE (model, prompt_sha256)
);
