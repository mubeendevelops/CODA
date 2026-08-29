-- Supersedes the role set from 000005_users: RBAC is admin | doctor |
-- reviewer | auditor (claude_context.md decision #30), not doctor | admin |
-- researcher. `researcher` is dropped; `reviewer` takes over clinical
-- review/edit/approve (previously implicit in `doctor`), `auditor` takes
-- over the old researcher's read-only, de-identified-only scope (audit_log
-- + aggregates, no source audio or unredacted transcript access).
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('admin', 'doctor', 'reviewer', 'auditor'));
