-- name: ClaimJobStage :one
-- Dedupe protocol step 2 (§2.3): claim the row before doing any work.
-- ON CONFLICT DO NOTHING means a retry/redelivery for the same
-- idempotency_key claims nothing new — the caller falls through to
-- GetJobStageByIdempotencyKey to find the existing row's status.
INSERT INTO job_stages (job_id, stage, idempotency_key, status, attempt)
VALUES ($1, $2, $3, 'running', 1)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *;

-- name: GetJobStageByIdempotencyKey :one
-- Dedupe protocol step 1 (§2.3): check for an already-succeeded row before
-- doing any work.
SELECT * FROM job_stages WHERE idempotency_key = $1;

-- name: ListJobStagesByJob :many
-- Query pattern: fetch a job with its stages — step 2 of 2.
SELECT * FROM job_stages WHERE job_id = $1 ORDER BY started_at NULLS LAST;

-- name: UpdateJobStageResult :one
UPDATE job_stages
SET status = $2, result_ref = $3, metrics = $4, finished_at = now()
WHERE id = $1
RETURNING *;

-- name: IncrementJobStageAttempt :one
UPDATE job_stages
SET attempt = attempt + 1, status = 'running', started_at = COALESCE(started_at, now())
WHERE id = $1
RETURNING *;

-- name: DispatchJobStage :one
-- Dedupe protocol (§2.3) from the orchestrator's side, as one statement.
--
-- A first dispatch inserts. A retry, a reaper re-dispatch, or a restarted
-- orchestrator re-running its scan hits the idempotency_key conflict and
-- updates the same row in place — attempt/deadline move, started_at and
-- error_history are preserved.
--
-- RunConfig interning (ADR-0012) means a DIFFERENT job (a resubmit after a
-- prior job dead-lettered or failed without this stage ever succeeding) can
-- compute the exact same idempotency_key — same consultation, stage,
-- run_config_id, and input. Without job_id in the SET list, that row stays
-- permanently owned by the old, terminal job: the new job's own
-- GET/SSE status view shows no stages at all, while the real work's
-- heartbeats and result land invisibly against the old job's row (found
-- live: a resubmitted job inherited attempt=4 from its dead-lettered
-- predecessor's exhausted 3-attempt budget, and the job actually running
-- showed zero progress to its own caller). Reassigning job_id here is what
-- makes a resubmit behave like the fresh job it is — the caller only passes
-- attempt=1 for it (dispatch.go), since inheriting an already-exhausted
-- attempt count would immediately dead-letter a job that never got to run.
-- started_at/error_history/last_error are preserved on a genuine same-job
-- retry (the common case this dedupe exists for) but reset when ownership
-- actually changes hands, so a new job's history doesn't carry a stranger's.
--
-- The DO UPDATE ... WHERE guard is the load-bearing part: a row already
-- 'succeeded' matches no update, so the statement returns *no rows*. That
-- is the caller's signal to skip dispatch entirely and reuse the stored
-- result_ref (GetJobStageByIdempotencyKey), which is what stops an
-- expensive stage being re-paid for after a crash — and, deliberately,
-- what lets a resubmitted job reuse a *successful* predecessor's result
-- without recomputing it. It is enforced by the UNIQUE constraint, not by
-- orchestrator correctness (ADR-0007).
INSERT INTO job_stages (job_id, stage, idempotency_key, status, attempt, deadline_at, started_at)
VALUES ($1, $2, $3, 'running', $4, $5, now())
ON CONFLICT (idempotency_key) DO UPDATE
SET job_id = EXCLUDED.job_id,
    status = 'running',
    attempt = EXCLUDED.attempt,
    deadline_at = EXCLUDED.deadline_at,
    started_at = CASE WHEN job_stages.job_id = EXCLUDED.job_id
                       THEN COALESCE(job_stages.started_at, now())
                       ELSE now()
                  END,
    finished_at = NULL,
    heartbeat_at = NULL,
    percent_complete = 0,
    step = NULL,
    last_error = CASE WHEN job_stages.job_id = EXCLUDED.job_id
                       THEN job_stages.last_error
                       ELSE NULL
                  END,
    error_history = CASE WHEN job_stages.job_id = EXCLUDED.job_id
                          THEN job_stages.error_history
                          ELSE '[]'::jsonb
                     END
WHERE job_stages.status <> 'succeeded'
RETURNING *;

-- name: GetJobStageByJobAndStage :one
-- Resolve a result/heartbeat message to its row when the sender echoed a
-- stale idempotency_key (a reclaimed message predating an input change).
-- Newest first: a stage re-run under a changed input has more than one row.
SELECT * FROM job_stages
WHERE job_id = $1 AND stage = $2
ORDER BY created_at DESC
LIMIT 1;

-- name: RecordJobStageHeartbeat :execrows
-- Lands one StageHeartbeat from stage.progress (§2.4). The orchestrator is
-- the only sanctioned consumer of stage.* streams (§1.2), so this row is
-- how progress reaches go-api's SSE endpoint — it polls Postgres and never
-- touches Redis (claude_context.md decision #35).
--
-- Only 'running' rows are updated: a heartbeat that arrives after the
-- result (reordering is legal on separate streams) must not resurrect a
-- finished stage or walk percent_complete back down from 100.
UPDATE job_stages
SET percent_complete = $3, step = $4, heartbeat_at = now()
WHERE job_id = $1 AND stage = $2 AND status = 'running';

-- name: CompleteJobStage :one
-- Terminal success write for one stage. percent_complete is forced to 100
-- so a UI reading the last heartbeat doesn't show a succeeded stage at 60%.
UPDATE job_stages
SET status = 'succeeded', result_ref = $2, metrics = $3,
    finished_at = now(), percent_complete = 100
WHERE id = $1
RETURNING *;

-- name: FailJobStage :one
-- Records one failed attempt. error_history is *appended* to, never
-- replaced — it is the DeadLetter.attempts[] field (§2.5: "carries the
-- original envelope, every attempt's error"), and a DLQ message must be
-- reconstructible from Postgres after Redis has trimmed the stream.
UPDATE job_stages
SET status = sqlc.arg(status),
    last_error = sqlc.arg(last_error),
    error_history = error_history || sqlc.arg(attempt_error)::jsonb,
    metrics = sqlc.arg(metrics),
    finished_at = now()
WHERE id = sqlc.arg(id)
RETURNING *;

-- name: ListStalledJobStages :many
-- §2.4's stall detection, the Postgres half. XAUTOCLAIM finds messages
-- idle in the PEL; this finds stages whose *worker* went quiet — past its
-- soft deadline (§4.2), or heartbeat-silent for longer than the allowed
-- gap even while still inside the visibility window.
SELECT * FROM job_stages
WHERE status = 'running'
  AND (
        (deadline_at IS NOT NULL AND deadline_at <= now())
     OR (heartbeat_at IS NOT NULL
         AND heartbeat_at < now() - make_interval(secs => @heartbeat_gap_seconds::double precision))
     OR (heartbeat_at IS NULL AND started_at IS NOT NULL
         AND started_at < now() - make_interval(secs => @startup_grace_seconds::double precision))
  )
ORDER BY started_at
LIMIT $1;
