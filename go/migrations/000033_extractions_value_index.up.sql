-- Fixes a silent data-loss bug present since Phase 4, found 2026-09-04 while
-- reading `write_pipeline_outputs` before building Module 6.
--
-- `extractions` was specified as "one row per field per run_config per
-- consultation" (architecture.md §5.3) with
-- UNIQUE (consultation_id, run_config_id, field_key, iteration). But five of
-- the nine field keys are LIST-valued (past_medical_history, medications,
-- allergies, provisional_diagnosis, investigations_advised) and the writer
-- inserts one row per item, every one of them with iteration = 0 and
-- ON CONFLICT ... DO UPDATE. Three past medical history items therefore
-- collided on the same key and only the last survived — the other two were
-- silently discarded.
--
-- Evaluation numbers were never affected: `coda_eval` scores the complete
-- `clinical_notes.note` JSON blob, not these rows. What was affected is the
-- thing architecture.md §5.3 says this table exists for — "per-field ablation
-- comparison as a plain GROUP BY" — which has been quietly reading truncated
-- lists.
--
-- `value_index` joins the unique key: item 0, 1, 2 of one field at one
-- iteration are now distinct rows that no longer overwrite each other.
-- Scalar fields keep value_index = 0 and are unaffected.

ALTER TABLE extractions
    ADD COLUMN value_index int NOT NULL DEFAULT 0 CHECK (value_index >= 0);

COMMENT ON COLUMN extractions.value_index IS
    'Position within a list-valued field (0 for scalar fields). Part of the uniqueness key: without it, list items collide and all but the last are lost.';

ALTER TABLE extractions DROP CONSTRAINT extractions_unique_field_per_run;
ALTER TABLE extractions
    ADD CONSTRAINT extractions_unique_field_per_run
        UNIQUE (consultation_id, run_config_id, field_key, iteration, value_index);

-- Module 5 records both a heuristic and an LLM-judge score for one candidate
-- when RunConfig.scorer_backend = 'both' (decision #96). `score_breakdown`
-- holds the breakdown that drove selection; this holds the other one, so a
-- query can compare the two scorers without parsing an artifact.
ALTER TABLE extractions
    ADD COLUMN secondary_score_breakdown jsonb;

-- Per-field inference cost, for the GoT-vs-single-pass cost comparison
-- (plan.md Phase 6 AC5). Kept on the row rather than only in
-- job_stages.metrics because the comparison is per FIELD, and job_stages
-- aggregates the whole stage.
ALTER TABLE extractions
    ADD COLUMN tokens_in int NOT NULL DEFAULT 0,
    ADD COLUMN tokens_out int NOT NULL DEFAULT 0,
    ADD COLUMN llm_calls int NOT NULL DEFAULT 0,
    ADD COLUMN cache_hits int NOT NULL DEFAULT 0;

-- Query pattern: "score trajectory for this field across iterations".
CREATE INDEX idx_extractions_field_iteration
    ON extractions (consultation_id, run_config_id, field_key, iteration);
