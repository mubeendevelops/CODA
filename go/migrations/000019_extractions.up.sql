-- One row per field per run_config per consultation, which is what makes
-- per-field ablation comparison a plain GROUP BY (§5.3).
CREATE TABLE extractions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id       uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id         uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    field_key             text NOT NULL CHECK (
        field_key IN (
            'chief_complaint', 'hopi', 'past_medical_history', 'medications',
            'allergies', 'examination_findings', 'provisional_diagnosis',
            'investigations_advised', 'treatment_plan'
        )
    ),
    value                 jsonb NOT NULL, -- FieldValue (proto/coda/v1/clinical.proto)
    candidate_set_uri     text,
    selected_candidate_idx int,
    score_breakdown       jsonb,
    iteration             int NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT extractions_unique_field_per_run
        UNIQUE (consultation_id, run_config_id, field_key, iteration)
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_extractions_consultation_run_config
    ON extractions (consultation_id, run_config_id, field_key);
