CREATE TABLE summaries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    text            text NOT NULL,
    rouge_l         double precision,
    bertscore       double precision,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT summaries_unique_per_run UNIQUE (consultation_id, run_config_id)
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_summaries_consultation_run_config
    ON summaries (consultation_id, run_config_id);
