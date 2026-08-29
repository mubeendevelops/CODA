-- audit_log is append-only (docs/architecture.md §7.4): an action cannot
-- succeed unaudited, and the audit trail itself cannot be altered after the
-- fact. Enforced here, not just by revoking UPDATE/DELETE grants, so the
-- invariant holds even for a role that has table-level privileges.
CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only: % on audit_log.id=% is not permitted', TG_OP, OLD.id;
END;
$$ LANGUAGE plpgsql;
