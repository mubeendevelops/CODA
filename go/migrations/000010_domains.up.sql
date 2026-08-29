-- Shared value domains, so the pipeline-state list (docs/architecture.md
-- §4.1) is defined once and reused by both consultations.state and
-- jobs.state, rather than repeated (and risking drift) in two CHECK lists.
-- A domain's constraint can be altered later (ALTER DOMAIN ... DROP/ADD
-- CONSTRAINT) without touching either table, which plain per-column CHECKs
-- would require doing twice.
CREATE DOMAIN pipeline_state AS text CHECK (
    VALUE IN (
        'created', 'consent_recorded', 'uploaded',
        'asr_queued', 'asr_running', 'asr_done',
        'redact_running', 'redacted',
        'nlp_queued', 'nlp_running', 'nlp_done',
        'awaiting_review', 'under_review', 'approved', 'exported',
        'failed', 'dead_lettered', 'cancelled'
    )
);

-- Stage identifies which pipeline stage a job/job_stage concerns
-- (proto/coda/v1/common.proto Stage enum, minus STAGE_UNSPECIFIED — a
-- persisted row should never be unspecified).
CREATE DOMAIN pipeline_stage AS text CHECK (
    VALUE IN ('asr', 'redact', 'nlp', 'export')
);
