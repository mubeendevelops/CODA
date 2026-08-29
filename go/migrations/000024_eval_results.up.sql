CREATE TABLE eval_results (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    eval_run_id           uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    consultation_id       uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id         uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    metric_key            text NOT NULL,
    metric_value          double precision NOT NULL,
    -- English and Kannada-English are never averaged into one number
    -- (claude_context.md §2, Phase 5/9 acceptance criteria).
    language              text NOT NULL CHECK (language IN ('en', 'kn_en')),
    -- Mandatory and non-null so an LLM-silver number can never be mistaken
    -- for a human-verified one when the results chapter is written (§5.5).
    reference_provenance  text NOT NULL CHECK (
        reference_provenance IN ('human_verified', 'llm_silver', 'dataset_gold')
    ),
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- Query pattern: aggregate eval results by run-config.
CREATE INDEX idx_eval_results_run_config_metric
    ON eval_results (run_config_id, metric_key);
CREATE INDEX idx_eval_results_eval_run_id ON eval_results (eval_run_id);
CREATE INDEX idx_eval_results_consultation_id ON eval_results (consultation_id);
