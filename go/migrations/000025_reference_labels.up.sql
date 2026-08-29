CREATE TABLE reference_labels (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id  uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    field_key        text NOT NULL CHECK (
        field_key IN (
            'chief_complaint', 'hopi', 'past_medical_history', 'medications',
            'allergies', 'examination_findings', 'provisional_diagnosis',
            'investigations_advised', 'treatment_plan'
        )
    ),
    value            jsonb NOT NULL,
    provenance       text NOT NULL CHECK (
        provenance IN ('human_verified', 'llm_silver', 'dataset_gold')
    ),
    -- Enforces decision #3 in code (ADR-0013): reference labels must come
    -- from a different model than the system under test. The equality
    -- check itself happens in application code at eval start, since it
    -- requires comparing against run_configs.config->>'base_model'; this
    -- column just records which model produced the label.
    generated_by_model text,
    verified_by_user   uuid REFERENCES users(id) ON DELETE SET NULL,
    verified_at        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_reference_labels_consultation_field
    ON reference_labels (consultation_id, field_key);
