-- One consultation can have multiple jobs — one per ablation arm
-- (run_config_id), since the eval sweep runs every arm over the same
-- consultation (docs/architecture.md §6.2). consultations.state mirrors
-- whichever job feeds the doctor-facing review UI; each job tracks its own
-- arm's progress independently through the same pipeline_state machine.
CREATE TABLE jobs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id  uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    state          pipeline_state NOT NULL DEFAULT 'created',
    current_stage  pipeline_stage,
    attempt        int NOT NULL DEFAULT 0,
    resume_after   timestamptz,
    error          jsonb,
    trace_id       text NOT NULL,
    eval_run_id    uuid REFERENCES eval_runs(id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_jobs_consultation_id ON jobs (consultation_id);
CREATE INDEX idx_jobs_run_config_id ON jobs (run_config_id);
CREATE INDEX idx_jobs_eval_run_id ON jobs (eval_run_id);
-- Orchestrator resume-scan pattern: jobs parked on a resume_after in a
-- given state.
CREATE INDEX idx_jobs_state_resume_after ON jobs (state, resume_after);

CREATE TRIGGER jobs_set_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
