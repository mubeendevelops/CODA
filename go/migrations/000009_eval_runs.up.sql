CREATE TABLE eval_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL,
    dataset_split       text NOT NULL,
    arm                 text NOT NULL,
    run_config_id       uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    started_at          timestamptz,
    finished_at         timestamptz,
    status              text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'paused', 'completed', 'failed')
    ),
    total_tokens        bigint NOT NULL DEFAULT 0,
    total_cost_estimate double precision NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_eval_runs_run_config_id ON eval_runs (run_config_id);

CREATE TRIGGER eval_runs_set_updated_at
    BEFORE UPDATE ON eval_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
