DROP INDEX IF EXISTS idx_job_stages_job_stage;
DROP INDEX IF EXISTS idx_job_stages_running_heartbeat;

ALTER TABLE job_stages
    DROP COLUMN error_history,
    DROP COLUMN last_error,
    DROP COLUMN deadline_at,
    DROP COLUMN heartbeat_at,
    DROP COLUMN step,
    DROP COLUMN percent_complete;
