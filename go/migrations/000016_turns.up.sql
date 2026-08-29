CREATE TABLE turns (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transcript_id   uuid NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    turn_index      int NOT NULL,
    speaker_label   text NOT NULL CHECK (speaker_label IN ('doctor', 'patient', 'unknown')),
    start_ms        int NOT NULL,
    end_ms          int NOT NULL,
    text            text NOT NULL,
    text_redacted   text, -- populated by REDACT_RUNNING (§7.3); only this ever enters a prompt
    confidence      real,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT turns_transcript_turn_index_unique UNIQUE (transcript_id, turn_index)
);

-- Query pattern: fetch a transcript's turns in order.
CREATE INDEX idx_turns_transcript_id ON turns (transcript_id, turn_index);
