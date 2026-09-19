-- The GoT ablation eval harness (`coda-eval run-got-eval`) builds thought
-- graphs directly from a dataset's own human reference transcript (PriMock57
-- clinician-collated turns), the same input `coda-eval run-baseline-eval`
-- already feeds to single-pass extraction. But unlike the baseline path,
-- Module 1/2's `graph/store.py:materialize_transcript` unconditionally
-- writes a `transcripts` row (thoughts.turn_id is a real FK into turns(id)),
-- and migration 000015's CHECK only allowed 'groq' | 'faster_whisper_local'
-- — both false statements about a row where no ASR ran at all.
--
-- 'dataset_gold' reuses the exact term `eval_results.reference_provenance`
-- already uses for "a real human reference, no model in the loop" (decision
-- #3b), rather than inventing a second vocabulary for the same idea.
ALTER TABLE transcripts DROP CONSTRAINT transcripts_asr_backend_check;
ALTER TABLE transcripts
    ADD CONSTRAINT transcripts_asr_backend_check
        CHECK (asr_backend IN ('groq', 'faster_whisper_local', 'dataset_gold'));
