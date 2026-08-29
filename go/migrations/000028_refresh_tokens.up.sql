-- Refresh-token rotation and revocation (docs/architecture.md §7.6): access
-- tokens are short-lived JWTs, never persisted; refresh tokens are opaque
-- random values, persisted only as a SHA-256 hash so a database read alone
-- never yields a usable credential.
--
-- Rotation chain: every refresh mints a new row and marks the old one
-- replaced_by_id + revoked_at. family_id is stable across a chain (equal to
-- the first token's own id) so that presenting an already-rotated token
-- (replaced_by_id IS NOT NULL) is a reuse signal — the caller revokes the
-- whole family, not just the one row, since reuse of a rotated-out token
-- means it leaked.
CREATE TABLE refresh_tokens (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id         uuid NOT NULL,
    token_hash        text NOT NULL,
    issued_at         timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    revoked_at        timestamptz,
    replaced_by_id    uuid REFERENCES refresh_tokens(id) ON DELETE SET NULL,
    created_by_ip     inet,
    user_agent        text,
    CONSTRAINT refresh_tokens_token_hash_unique UNIQUE (token_hash)
);

CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX idx_refresh_tokens_family_id ON refresh_tokens (family_id);
