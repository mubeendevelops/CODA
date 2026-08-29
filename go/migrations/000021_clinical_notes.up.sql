CREATE TABLE clinical_notes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    version         int NOT NULL DEFAULT 1,
    status          text NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'under_review', 'approved')
    ),
    note            jsonb NOT NULL, -- ClinicalNote (proto/coda/v1/clinical.proto)
    approved_by     uuid REFERENCES users(id) ON DELETE SET NULL,
    approved_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT clinical_notes_unique_version
        UNIQUE (consultation_id, run_config_id, version),
    -- No auto-save before doctor approval, enforced at the schema layer
    -- (§7.5): approved_at/approved_by are set if and only if status is
    -- 'approved'.
    CONSTRAINT clinical_notes_approval_chk CHECK (
        (status = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR (status <> 'approved' AND approved_by IS NULL AND approved_at IS NULL)
    )
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_clinical_notes_consultation_run_config
    ON clinical_notes (consultation_id, run_config_id);

CREATE TRIGGER clinical_notes_set_updated_at
    BEFORE UPDATE ON clinical_notes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
