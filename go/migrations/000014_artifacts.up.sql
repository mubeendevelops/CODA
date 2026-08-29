-- artifacts records MinIO object references (docs/architecture.md §3.2-3.3):
-- the actual bytes live in MinIO, this table is the durable pointer to them.
-- No updated_at trigger: artifact keys are immutable once written (§3.3) —
-- a re-run under a changed config gets a new run_config_id, hence a new
-- row, never an update to an existing one.
CREATE TABLE artifacts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    stage           pipeline_stage NOT NULL,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    kind            text NOT NULL CHECK (
        kind IN (
            'audio', 'consent', 'transcript', 'thought_graph', 'candidate_set',
            'clinical_note', 'summary', 'export_json', 'export_pdf', 'metrics',
            'redaction_map'
        )
    ),
    uri             text NOT NULL,
    sha256          text NOT NULL,
    bytes           bigint NOT NULL,
    content_type    text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifacts_uri_unique UNIQUE (uri)
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_artifacts_consultation_run_config
    ON artifacts (consultation_id, run_config_id);
