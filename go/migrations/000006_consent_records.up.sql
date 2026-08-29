-- consent_records is created before consultations because
-- consultations.consent_record_id is a mandatory (NOT NULL) foreign key
-- into it (docs/architecture.md §7.2) — a consultation cannot be
-- represented in the schema without consent.
CREATE TABLE consent_records (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE RESTRICT,
    subject_ref      text NOT NULL, -- pseudonymous, never a name (§7.2)
    consent_type     text NOT NULL,
    granted_at       timestamptz NOT NULL,
    granted_by       uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    scope            jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_uri     text,
    revoked_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_consent_records_org_id ON consent_records (org_id);

CREATE TRIGGER consent_records_set_updated_at
    BEFORE UPDATE ON consent_records
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
