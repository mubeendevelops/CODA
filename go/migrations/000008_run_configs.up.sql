-- run_configs is the ablation configuration object (docs/architecture.md §6,
-- ADR-0012). Configs are interned: content_hash is UNIQUE, so inserting an
-- existing config returns the existing row rather than duplicating it.
-- No updated_at/no update trigger: a RunConfig is immutable once created —
-- changing a knob mints a new content_hash, hence a new row.
CREATE TABLE run_configs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash   text NOT NULL,
    arm            text NOT NULL CHECK (
        arm IN ('baseline', 'got_k1', 'got_k2', 'got_k2_nograph', 'got_k2_kg', 'frontier_ref')
    ),
    config         jsonb NOT NULL, -- canonical RunConfig (proto/coda/v1/runconfig.proto)
    schema_version int NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_configs_content_hash_unique UNIQUE (content_hash)
);

CREATE INDEX idx_run_configs_arm ON run_configs (arm);
