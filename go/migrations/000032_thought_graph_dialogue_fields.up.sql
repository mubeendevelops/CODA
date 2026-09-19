-- Phase 6, Modules 1-2: the columns dialogue-derived thoughts need that
-- prose-EHR thoughts did not (claude_context.md §6, hypothesis H3).
--
-- architecture.md §5.3 specified `thoughts` without polarity, confidence, or
-- a sub-turn span, and `thought_edges` with exactly three edge types. Both
-- were written against GoT-HCS's prose-EHR node construction. Conversational
-- input breaks both assumptions: a symptom asserted by the patient in one
-- turn and negated by the doctor eight turns later must survive as two
-- linked thoughts (otherwise distillation cannot tell "chest pain" from "no
-- chest pain"), and a single turn routinely carries several distinct
-- clinical thoughts, so the turn alone is not adequate provenance.

ALTER TABLE thoughts
    ADD COLUMN polarity text NOT NULL DEFAULT 'asserted'
        CHECK (polarity IN ('asserted', 'negated', 'uncertain', 'hypothetical')),
    -- 0.0-1.0: how directly the source turn supports this thought. Nullable
    -- because a rule-derived thought has no model confidence to report.
    ADD COLUMN confidence real CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    -- [char_start, char_end) into the source turn's redacted text.
    ADD COLUMN char_start int CHECK (char_start IS NULL OR char_start >= 0),
    ADD COLUMN char_end int CHECK (char_end IS NULL OR char_end >= 0),
    -- Denormalized from turns.turn_index. Reachable by join, duplicated
    -- anyway because turn_index is the identifier the transcript, the
    -- prompts and every FieldValue.source_turn_ids speak in, and because
    -- `thoughts` rows are written once per (consultation, run_config) and
    -- never updated, so the copy cannot drift.
    ADD COLUMN turn_index int,
    ADD CONSTRAINT thoughts_span_ordered
        CHECK (char_start IS NULL OR char_end IS NULL OR char_end >= char_start);

COMMENT ON COLUMN thoughts.polarity IS
    'Assertion status. Distinguishes "no chest pain" from "chest pain"; uncertain/hypothetical are kept separate from asserted so hedged speech is never distilled as fact (claude_context.md §3).';

-- Query pattern: graph-retrieval seeding selects by (consultation, run_config, category).
CREATE INDEX idx_thoughts_category
    ON thoughts (consultation_id, run_config_id, category);

-- Three edge types added for dialogue. NEGATION links the asserted thought
-- to the thought that denies it; ELABORATION links a symptom's fragments
-- across non-adjacent turns; COREFERENCE resolves "it"/"that" across an
-- interruption. See coda/v1/thought.proto's EdgeType for the rationale.
ALTER TABLE thought_edges DROP CONSTRAINT thought_edges_edge_type_check;
ALTER TABLE thought_edges
    ADD CONSTRAINT thought_edges_edge_type_check CHECK (
        edge_type IN (
            'temporal', 'causal', 'logical',
            'negation', 'elaboration', 'coreference'
        )
    ),
    -- One short justification per edge, for the case-study figures and for
    -- auditing whether LLM-predicted edges are defensible. Never read by
    -- retrieval or scoring.
    ADD COLUMN rationale text;

-- nlp-service materializes `transcripts`/`turns` from the redacted
-- transcript artifact before writing thoughts, so thoughts.turn_id has real
-- rows to reference (nothing populated either table before this: the sqlc
-- queries existed and were called from nowhere). At-least-once delivery
-- (architecture.md §2.3) means that materialization can run twice for one
-- (consultation, run_config), so it needs a conflict target. This supersedes
-- 000015's "a re-run produces a new row" comment: a re-run of the SAME
-- run_config is a redelivery, not a new experiment, and must be idempotent —
-- a different run_config still gets its own row, which is what the ablation
-- actually needs.
ALTER TABLE transcripts
    ADD CONSTRAINT transcripts_consultation_run_config_unique
        UNIQUE (consultation_id, run_config_id);
