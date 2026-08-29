-- v1 is English-only (claude_context.md §2.1, 2026-08-29 rescope). This
-- default is a safety net for any insert path that doesn't specify
-- language explicitly, not a behavior change for existing callers: the
-- CreateConsultation query already passes language as an explicit param.
-- The CHECK constraint from migration 000011 already allows 'kn_en' — the
-- column was language-agnostic before this migration and stays so after
-- it; only the default is new.
ALTER TABLE consultations ALTER COLUMN language SET DEFAULT 'en';
