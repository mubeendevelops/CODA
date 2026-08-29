"""Stream names, consumer groups, field names, and message kinds.

Verbatim mirror of go/internal/queue/streams.go — the two sides hard-code the
same strings (docs/architecture.md §2.1), so this file must be kept in sync
with that one by hand whenever the contract changes. There is no shared
source of truth beyond the two files agreeing.
"""

from __future__ import annotations

from coda.v1 import common_pb2

# --- stream + group names (go/internal/queue/streams.go) -------------------

STREAM_ASR = "stage.asr"
STREAM_NLP = "stage.nlp"
STREAM_RESULTS = "stage.results"
STREAM_PROGRESS = "stage.progress"
STREAM_DLQ = "stage.dlq"
STREAM_JOB_SUBMITTED = "job.submitted"

GROUP_ASR_WORKERS = "asr-workers"
GROUP_NLP_WORKERS = "nlp-workers"
GROUP_ORCHESTRATOR = "orchestrator"

# --- stream entry field names ----------------------------------------------

FIELD_PAYLOAD = "payload"
FIELD_KIND = "kind"
FIELD_SCHEMA_VERSION = "schema_version"
FIELD_IDEMPOTENCY_KEY = "idempotency_key"
FIELD_TRACE_ID = "trace_id"
FIELD_JOB_ID = "job_id"
FIELD_STAGE = "stage"
FIELD_ATTEMPT = "attempt"

# --- message kinds ----------------------------------------------------------

KIND_STAGE_ENVELOPE = "StageEnvelope"
KIND_STAGE_RESULT = "StageResult"
KIND_STAGE_HEARTBEAT = "StageHeartbeat"
KIND_DEAD_LETTER = "DeadLetter"
KIND_JOB_SUBMITTED = "JobSubmitted"

# --- schema version ----------------------------------------------------------

SCHEMA_VERSION = 1
"""Bumped on any breaking envelope/payload contract change (architecture.md
§2.2, §3.1). A message declaring a version this build does not implement is
rejected FATAL / SCHEMA_UNSUPPORTED, never guessed at."""

# --- Stage <-> pipeline_stage column name ------------------------------------

_STAGE_NAMES: dict[common_pb2.Stage, str] = {
    common_pb2.Stage.STAGE_ASR: "asr",
    common_pb2.Stage.STAGE_REDACT: "redact",
    common_pb2.Stage.STAGE_NLP: "nlp",
    common_pb2.Stage.STAGE_EXPORT: "export",
}
_STAGES_BY_NAME: dict[str, common_pb2.Stage] = {v: k for k, v in _STAGE_NAMES.items()}


def stage_name(stage: common_pb2.Stage) -> str:
    """The pipeline_stage column value for a proto Stage. Mirrors
    go/internal/queue/streams.go's StageName."""
    return _STAGE_NAMES.get(stage, "")


def stage_from_name(name: str) -> common_pb2.Stage:
    try:
        return _STAGES_BY_NAME[name]
    except KeyError as exc:
        raise ValueError(f"coda_worker_sdk: {name!r} is not a pipeline stage") from exc


def group_for_stream(stream: str) -> str:
    """The consumer group that drains a request stream. Mirrors
    go/internal/queue/streams.go's GroupFor."""
    if stream == STREAM_ASR:
        return GROUP_ASR_WORKERS
    if stream == STREAM_NLP:
        return GROUP_NLP_WORKERS
    if stream in (STREAM_RESULTS, STREAM_PROGRESS, STREAM_JOB_SUBMITTED):
        return GROUP_ORCHESTRATOR
    raise ValueError(f"coda_worker_sdk: no consumer group for stream {stream!r}")
