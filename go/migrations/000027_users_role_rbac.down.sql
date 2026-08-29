-- Reversible only if no row currently holds a role outside the old set —
-- matches every other migration's up/down symmetry contract, not a
-- promise that down is safe after real reviewer/auditor rows exist.
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('doctor', 'admin', 'researcher'));
