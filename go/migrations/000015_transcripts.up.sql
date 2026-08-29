-- No updated_at trigger: a transcript is a stage output artifact, written
-- once per (consultation, run_config), not mutated in place — a re-run
-- produces a new row (architecture.md §4.3 checkpoint model).
CREATE TABLE transcripts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    uri             text NOT NULL,
    asr_backend     text NOT NULL CHECK (asr_backend IN ('groq', 'faster_whisper_local')),
    asr_model       text NOT NULL,
    wer             double precision,
    der             double precision,
    language        text NOT NULL CHECK (language IN ('en', 'kn_en')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Query pattern: fetch full pipeline artifacts for one consultation.
CREATE INDEX idx_transcripts_consultation_run_config
    ON transcripts (consultation_id, run_config_id);
