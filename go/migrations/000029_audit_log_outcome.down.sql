DROP INDEX IF EXISTS idx_audit_log_outcome;
ALTER TABLE audit_log DROP COLUMN IF EXISTS outcome;
