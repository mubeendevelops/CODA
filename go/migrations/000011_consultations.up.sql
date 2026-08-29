CREATE TABLE consultations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE RESTRICT,
    owner_user_id      uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    -- Consent is a schema invariant, not an application check (§7.2): a
    -- consultation without consent is not representable. consent_obtained
    -- defaults false and is never nullable, so "unknown" cannot be
    -- mistaken for "granted".
    consent_record_id  uuid NOT NULL REFERENCES consent_records(id) ON DELETE RESTRICT,
    consent_obtained   boolean NOT NULL DEFAULT false,
    consent_method     text,
    consent_recorded_at timestamptz,

    state              pipeline_state NOT NULL DEFAULT 'created',
    language           text NOT NULL CHECK (language IN ('en', 'kn_en')),
    source_audio_uri   text,
    audio_sha256       text,
    duration_sec       double precision,
    cancel_requested   boolean NOT NULL DEFAULT false,

    -- DPDP erasure tombstone (§7.2): on consent withdrawal, the erasure job
    -- deletes derived MinIO artifacts, nulls the PII-bearing columns above
    -- (source_audio_uri, audio_sha256), and sets erased_at — the row itself
    -- is retained as the tombstone, never hard-deleted, so the audit trail
    -- stays attributable. Everywhere else in the schema, "soft delete"
    -- doesn't apply: no other table has an independent retention policy
    -- distinct from the consultation that owns it.
    erased_at          timestamptz,

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_consultations_org_id ON consultations (org_id);
CREATE INDEX idx_consultations_owner_user_id ON consultations (owner_user_id);
CREATE INDEX idx_consultations_consent_record_id ON consultations (consent_record_id);

-- Query pattern: list consultations by org with a status filter, most
-- recent first.
CREATE INDEX idx_consultations_org_state_created
    ON consultations (org_id, state, created_at DESC);

CREATE TRIGGER consultations_set_updated_at
    BEFORE UPDATE ON consultations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
