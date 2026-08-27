from coda.v1 import common_pb2 as _common_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class StageEnvelope(_message.Message):
    __slots__ = ("job_id", "consultation_id", "stage", "attempt", "idempotency_key", "trace_id", "schema_version", "run_config_id", "payload_ref", "enqueued_at", "deadline", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REF_FIELD_NUMBER: _ClassVar[int]
    ENQUEUED_AT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    consultation_id: str
    stage: _common_pb2.Stage
    attempt: int
    idempotency_key: str
    trace_id: str
    schema_version: int
    run_config_id: str
    payload_ref: str
    enqueued_at: _timestamp_pb2.Timestamp
    deadline: _timestamp_pb2.Timestamp
    labels: _containers.ScalarMap[str, str]
    def __init__(self, job_id: _Optional[str] = ..., consultation_id: _Optional[str] = ..., stage: _Optional[_Union[_common_pb2.Stage, str]] = ..., attempt: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., trace_id: _Optional[str] = ..., schema_version: _Optional[int] = ..., run_config_id: _Optional[str] = ..., payload_ref: _Optional[str] = ..., enqueued_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., deadline: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class StageResult(_message.Message):
    __slots__ = ("job_id", "consultation_id", "stage", "attempt", "idempotency_key", "trace_id", "schema_version", "status", "result_ref", "error", "metrics", "resume_after")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULT_REF_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    METRICS_FIELD_NUMBER: _ClassVar[int]
    RESUME_AFTER_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    consultation_id: str
    stage: _common_pb2.Stage
    attempt: int
    idempotency_key: str
    trace_id: str
    schema_version: int
    status: _common_pb2.Status
    result_ref: str
    error: _common_pb2.Error
    metrics: _common_pb2.StageMetrics
    resume_after: _timestamp_pb2.Timestamp
    def __init__(self, job_id: _Optional[str] = ..., consultation_id: _Optional[str] = ..., stage: _Optional[_Union[_common_pb2.Stage, str]] = ..., attempt: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., trace_id: _Optional[str] = ..., schema_version: _Optional[int] = ..., status: _Optional[_Union[_common_pb2.Status, str]] = ..., result_ref: _Optional[str] = ..., error: _Optional[_Union[_common_pb2.Error, _Mapping]] = ..., metrics: _Optional[_Union[_common_pb2.StageMetrics, _Mapping]] = ..., resume_after: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class StageHeartbeat(_message.Message):
    __slots__ = ("job_id", "stage", "attempt", "percent_complete", "step", "trace_id", "at")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    PERCENT_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    STEP_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    AT_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    stage: _common_pb2.Stage
    attempt: int
    percent_complete: float
    step: str
    trace_id: str
    at: _timestamp_pb2.Timestamp
    def __init__(self, job_id: _Optional[str] = ..., stage: _Optional[_Union[_common_pb2.Stage, str]] = ..., attempt: _Optional[int] = ..., percent_complete: _Optional[float] = ..., step: _Optional[str] = ..., trace_id: _Optional[str] = ..., at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AttemptError(_message.Message):
    __slots__ = ("attempt", "error", "occurred_at")
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    attempt: int
    error: _common_pb2.Error
    occurred_at: _timestamp_pb2.Timestamp
    def __init__(self, attempt: _Optional[int] = ..., error: _Optional[_Union[_common_pb2.Error, _Mapping]] = ..., occurred_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class DeadLetter(_message.Message):
    __slots__ = ("job_id", "consultation_id", "stage", "original_envelope", "attempts", "final_status", "dead_lettered_at")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    FINAL_STATUS_FIELD_NUMBER: _ClassVar[int]
    DEAD_LETTERED_AT_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    consultation_id: str
    stage: _common_pb2.Stage
    original_envelope: StageEnvelope
    attempts: _containers.RepeatedCompositeFieldContainer[AttemptError]
    final_status: _common_pb2.Status
    dead_lettered_at: _timestamp_pb2.Timestamp
    def __init__(self, job_id: _Optional[str] = ..., consultation_id: _Optional[str] = ..., stage: _Optional[_Union[_common_pb2.Stage, str]] = ..., original_envelope: _Optional[_Union[StageEnvelope, _Mapping]] = ..., attempts: _Optional[_Iterable[_Union[AttemptError, _Mapping]]] = ..., final_status: _Optional[_Union[_common_pb2.Status, str]] = ..., dead_lettered_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
