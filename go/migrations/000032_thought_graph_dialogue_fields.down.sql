ALTER TABLE transcripts DROP CONSTRAINT transcripts_consultation_run_config_unique;

ALTER TABLE thought_edges DROP COLUMN rationale;
ALTER TABLE thought_edges DROP CONSTRAINT thought_edges_edge_type_check;
-- Rows carrying a dialogue-only edge type cannot satisfy the restored
-- three-value constraint; drop them rather than fail the migration.
DELETE FROM thought_edges
    WHERE edge_type IN ('negation', 'elaboration', 'coreference');
ALTER TABLE thought_edges
    ADD CONSTRAINT thought_edges_edge_type_check
        CHECK (edge_type IN ('temporal', 'causal', 'logical'));

DROP INDEX IF EXISTS idx_thoughts_category;
ALTER TABLE thoughts
    DROP CONSTRAINT thoughts_span_ordered,
    DROP COLUMN turn_index,
    DROP COLUMN char_end,
    DROP COLUMN char_start,
    DROP COLUMN confidence,
    DROP COLUMN polarity;
