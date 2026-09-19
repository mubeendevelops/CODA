DROP INDEX IF EXISTS idx_extractions_field_iteration;

ALTER TABLE extractions
    DROP COLUMN cache_hits,
    DROP COLUMN llm_calls,
    DROP COLUMN tokens_out,
    DROP COLUMN tokens_in,
    DROP COLUMN secondary_score_breakdown;

-- Restoring the narrower unique key requires that no two surviving rows
-- share (consultation_id, run_config_id, field_key, iteration). Keep the
-- lowest value_index of each group — which is the row the buggy pre-000033
-- writer would itself have been left with, so this reproduces the old
-- (lossy) state rather than failing the migration.
DELETE FROM extractions e
    USING extractions keep
    WHERE e.consultation_id = keep.consultation_id
      AND e.run_config_id = keep.run_config_id
      AND e.field_key = keep.field_key
      AND e.iteration = keep.iteration
      AND keep.value_index < e.value_index;

ALTER TABLE extractions DROP CONSTRAINT extractions_unique_field_per_run;
ALTER TABLE extractions DROP COLUMN value_index;
ALTER TABLE extractions
    ADD CONSTRAINT extractions_unique_field_per_run
        UNIQUE (consultation_id, run_config_id, field_key, iteration);
