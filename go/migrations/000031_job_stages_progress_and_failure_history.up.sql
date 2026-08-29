-- job_stages gains the columns the orchestrator needs to (a) surface
-- worker progress without go-api ever touching a stage.* stream and (b)
-- build a DeadLetter carrying the *full* failure history.
--
-- (a) Progress. docs/architecture.md §2.4 has workers emit StageHeartbeat
-- to stage.progress every 15s. §1.2 forbids go-api from consuming stage.*
-- streams, so the orchestrator — the stream's only sanctioned consumer —
-- lands each heartbeat here, and go-api's SSE endpoint keeps reading
-- Postgres exactly as claude_context.md decision #35 describes. The
-- backing mechanism the decision anticipated changing is the *source* of
-- the data, not the endpoint's read path.
--
-- (b) Failure history. §2.5: "a DeadLetter in stage.dlq carries the
-- original envelope, every attempt's error, the full trace". `attempt` is
-- a counter and cannot reconstruct what each attempt failed with, so
-- error_history accumulates one AttemptError-shaped object per failed
-- attempt. It lives on job_stages rather than in Redis because the DLQ
-- message must be reconstructible from Postgres alone after the stream is
-- trimmed (§0: "every result reconstructible from the database").
ALTER TABLE job_stages
    -- Latest heartbeat's percent_complete/step (proto StageHeartbeat).
    ADD COLUMN percent_complete real NOT NULL DEFAULT 0
        CHECK (percent_complete >= 0 AND percent_complete <= 100),
    ADD COLUMN step             text,
    -- When the last heartbeat arrived. NULL = none yet this attempt.
    -- §2.4's "heartbeats stopped more than 90s ago is stalled even inside
    -- the visibility window" check reads this column.
    ADD COLUMN heartbeat_at     timestamptz,
    -- Absolute soft deadline for the in-flight attempt (§4.2), stamped at
    -- dispatch. Durable rather than in-memory so a restarted orchestrator
    -- still enforces the timeout of a stage dispatched by its predecessor.
    ADD COLUMN deadline_at      timestamptz,
    -- The most recent attempt's Error, for the common "why is this stuck"
    -- SELECT that shouldn't have to index into a JSON array.
    ADD COLUMN last_error       jsonb,
    -- Append-only array of {attempt, error, occurred_at} — the DeadLetter
    -- attempts[] field verbatim.
    ADD COLUMN error_history    jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(error_history) = 'array');

-- Stale-attempt scan: "running stages whose heartbeat or deadline has
-- lapsed". Partial index — only `running` rows are ever scanned, and they
-- are a tiny fraction of the table once an eval sweep has accumulated
-- history.
CREATE INDEX idx_job_stages_running_heartbeat
    ON job_stages (heartbeat_at, deadline_at)
    WHERE status = 'running';

-- The orchestrator resolves a result message to its row by (job, stage);
-- idempotency_key is unique but a result for a *superseded* input hash
-- must still find the row it belongs to.
CREATE INDEX idx_job_stages_job_stage ON job_stages (job_id, stage);
