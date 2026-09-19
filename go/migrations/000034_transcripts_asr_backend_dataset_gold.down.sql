-- Reversing requires no surviving row to use the value being removed.
DELETE FROM transcripts WHERE asr_backend = 'dataset_gold';

ALTER TABLE transcripts DROP CONSTRAINT transcripts_asr_backend_check;
ALTER TABLE transcripts
    ADD CONSTRAINT transcripts_asr_backend_check
        CHECK (asr_backend IN ('groq', 'faster_whisper_local'));
