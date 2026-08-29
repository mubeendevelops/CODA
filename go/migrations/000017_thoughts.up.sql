CREATE TABLE thoughts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    turn_id         uuid NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    speaker         text NOT NULL CHECK (speaker IN ('doctor', 'patient', 'unknown')),
    text            text NOT NULL,
    entities        jsonb NOT NULL DEFAULT '[]'::jsonb,
    category        text NOT NULL CHECK (
        category IN (
            'symptom', 'history', 'medication', 'allergy', 'examination',
            'diagnosis', 'investigation', 'plan', 'other'
        )
    ),
    temporal_anchor text,
    linked_concepts jsonb NOT NULL DEFAULT '[]'::jsonb, -- MeSH / ICD-10 identifiers
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_thoughts_consultation_run_config
    ON thoughts (consultation_id, run_config_id);
CREATE INDEX idx_thoughts_turn_id ON thoughts (turn_id);
