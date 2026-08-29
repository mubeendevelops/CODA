-- audit_log has no updated_at / no update trigger by design: it is
-- append-only (enforced below), so "last modified" is meaningless.
CREATE TABLE audit_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE RESTRICT,
    actor_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
    actor_service  text,
    action         text NOT NULL,
    resource_type  text NOT NULL,
    resource_id    text NOT NULL,
    before         jsonb,
    after          jsonb,
    trace_id       text NOT NULL,
    ip             inet,
    at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT audit_log_actor_chk CHECK (
        (actor_user_id IS NOT NULL) OR (actor_service IS NOT NULL)
    )
);

CREATE INDEX idx_audit_log_org_at ON audit_log (org_id, at DESC);
CREATE INDEX idx_audit_log_resource ON audit_log (resource_type, resource_id);

-- Append-only: reject UPDATE/DELETE at the trigger layer, not only via
-- revoked grants (docs/architecture.md §7.4).
CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
