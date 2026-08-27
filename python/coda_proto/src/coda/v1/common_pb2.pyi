from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Stage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STAGE_UNSPECIFIED: _ClassVar[Stage]
    STAGE_ASR: _ClassVar[Stage]
    STAGE_REDACT: _ClassVar[Stage]
    STAGE_NLP: _ClassVar[Stage]
    STAGE_EXPORT: _ClassVar[Stage]

class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STATUS_UNSPECIFIED: _ClassVar[Status]
    STATUS_OK: _ClassVar[Status]
    STATUS_RETRYABLE: _ClassVar[Status]
    STATUS_FATAL: _ClassVar[Status]
    STATUS_QUOTA_EXHAUSTED: _ClassVar[Status]
    STATUS_CANCELLED: _ClassVar[Status]

class ArtifactKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARTIFACT_KIND_UNSPECIFIED: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_AUDIO: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_CONSENT: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_TRANSCRIPT: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_THOUGHT_GRAPH: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_CANDIDATE_SET: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_CLINICAL_NOTE: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_SUMMARY: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_EXPORT_JSON: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_EXPORT_PDF: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_METRICS: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_REDACTION_MAP: _ClassVar[ArtifactKind]
STAGE_UNSPECIFIED: Stage
STAGE_ASR: Stage
STAGE_REDACT: Stage
STAGE_NLP: Stage
STAGE_EXPORT: Stage
STATUS_UNSPECIFIED: Status
STATUS_OK: Status
STATUS_RETRYABLE: Status
STATUS_FATAL: Status
STATUS_QUOTA_EXHAUSTED: Status
STATUS_CANCELLED: Status
ARTIFACT_KIND_UNSPECIFIED: ArtifactKind
ARTIFACT_KIND_AUDIO: ArtifactKind
ARTIFACT_KIND_CONSENT: ArtifactKind
ARTIFACT_KIND_TRANSCRIPT: ArtifactKind
ARTIFACT_KIND_THOUGHT_GRAPH: ArtifactKind
ARTIFACT_KIND_CANDIDATE_SET: ArtifactKind
ARTIFACT_KIND_CLINICAL_NOTE: ArtifactKind
ARTIFACT_KIND_SUMMARY: ArtifactKind
ARTIFACT_KIND_EXPORT_JSON: ArtifactKind
ARTIFACT_KIND_EXPORT_PDF: ArtifactKind
ARTIFACT_KIND_METRICS: ArtifactKind
ARTIFACT_KIND_REDACTION_MAP: ArtifactKind

class Error(_message.Message):
    __slots__ = ("code", "message", "retryable", "provider_status")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_STATUS_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    retryable: bool
    provider_status: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: bool = ..., provider_status: _Optional[str] = ...) -> None: ...

class StageMetrics(_message.Message):
    __slots__ = ("tokens_in", "tokens_out", "llm_calls", "cache_hits", "wall_ms", "model_ids", "cost_estimate")
    TOKENS_IN_FIELD_NUMBER: _ClassVar[int]
    TOKENS_OUT_FIELD_NUMBER: _ClassVar[int]
    LLM_CALLS_FIELD_NUMBER: _ClassVar[int]
    CACHE_HITS_FIELD_NUMBER: _ClassVar[int]
    WALL_MS_FIELD_NUMBER: _ClassVar[int]
    MODEL_IDS_FIELD_NUMBER: _ClassVar[int]
    COST_ESTIMATE_FIELD_NUMBER: _ClassVar[int]
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int
    wall_ms: int
    model_ids: _containers.RepeatedScalarFieldContainer[str]
    cost_estimate: float
    def __init__(self, tokens_in: _Optional[int] = ..., tokens_out: _Optional[int] = ..., llm_calls: _Optional[int] = ..., cache_hits: _Optional[int] = ..., wall_ms: _Optional[int] = ..., model_ids: _Optional[_Iterable[str]] = ..., cost_estimate: _Optional[float] = ...) -> None: ...

class ArtifactRef(_message.Message):
    __slots__ = ("uri", "sha256", "bytes", "content_type", "kind")
    URI_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    uri: str
    sha256: str
    bytes: int
    content_type: str
    kind: ArtifactKind
    def __init__(self, uri: _Optional[str] = ..., sha256: _Optional[str] = ..., bytes: _Optional[int] = ..., content_type: _Optional[str] = ..., kind: _Optional[_Union[ArtifactKind, str]] = ...) -> None: ...
