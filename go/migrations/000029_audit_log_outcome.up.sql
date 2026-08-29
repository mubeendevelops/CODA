-- The task spec requires audit entries to record actor, action, resource,
-- org, IP, and outcome. before/after jsonb existed for state diffs but had
-- nowhere typed to put "did this action succeed" — outcome is a fact about
-- every entry, not free-form diff payload, so it gets its own column
-- rather than being smuggled into `after`.
ALTER TABLE audit_log
    ADD COLUMN outcome text NOT NULL DEFAULT 'success'
        CHECK (outcome IN ('success', 'failure'));

-- The default above exists only so the column can be added without
-- rewriting hypothetical pre-existing rows; every future insert supplies
-- outcome explicitly (go/internal/audit records it from the response
-- status), so the default is not meant to be relied on going forward.
ALTER TABLE audit_log ALTER COLUMN outcome DROP DEFAULT;

CREATE INDEX idx_audit_log_outcome ON audit_log (outcome) WHERE outcome = 'failure';
