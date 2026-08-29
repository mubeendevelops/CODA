-- One row per unit of work (docs/architecture.md §2.3): a retry updates
-- attempt/status in place on the same row rather than inserting a new one,
-- because idempotency_key is stable across redeliveries of the same
-- (consultation, stage, run_config, input) work-unit.
CREATE TABLE job_stages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage           pipeline_stage NOT NULL,
    idempotency_key text NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'quota_exhausted', 'cancelled')
    ),
    attempt         int NOT NULL DEFAULT 0,
    result_ref      text,
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb, -- tokens, llm_calls, cache_hits, wall_ms
    started_at      timestamptz,
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Database-level enforcement of the dedupe guarantee (ADR-0007): it
    -- must hold even if worker code is wrong.
    CONSTRAINT job_stages_idempotency_key_unique UNIQUE (idempotency_key)
);

-- Query pattern: fetch a job with its stages.
CREATE INDEX idx_job_stages_job_id ON job_stages (job_id);

CREATE TRIGGER job_stages_set_updated_at
    BEFORE UPDATE ON job_stages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
