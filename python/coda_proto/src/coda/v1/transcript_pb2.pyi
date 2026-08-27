from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SpeakerRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPEAKER_ROLE_UNSPECIFIED: _ClassVar[SpeakerRole]
    SPEAKER_ROLE_DOCTOR: _ClassVar[SpeakerRole]
    SPEAKER_ROLE_PATIENT: _ClassVar[SpeakerRole]
    SPEAKER_ROLE_UNKNOWN: _ClassVar[SpeakerRole]
SPEAKER_ROLE_UNSPECIFIED: SpeakerRole
SPEAKER_ROLE_DOCTOR: SpeakerRole
SPEAKER_ROLE_PATIENT: SpeakerRole
SPEAKER_ROLE_UNKNOWN: SpeakerRole

class Word(_message.Message):
    __slots__ = ("text", "start_ms", "end_ms", "confidence", "speaker_cluster_id")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    text: str
    start_ms: int
    end_ms: int
    confidence: float
    speaker_cluster_id: str
    def __init__(self, text: _Optional[str] = ..., start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ..., confidence: _Optional[float] = ..., speaker_cluster_id: _Optional[str] = ...) -> None: ...

class SpeakerCluster(_message.Message):
    __slots__ = ("cluster_id", "assigned_role", "confidence")
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_ROLE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    cluster_id: str
    assigned_role: SpeakerRole
    confidence: float
    def __init__(self, cluster_id: _Optional[str] = ..., assigned_role: _Optional[_Union[SpeakerRole, str]] = ..., confidence: _Optional[float] = ...) -> None: ...

class Turn(_message.Message):
    __slots__ = ("turn_index", "speaker_label", "start_ms", "end_ms", "text", "text_redacted", "confidence", "words")
    TURN_INDEX_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_LABEL_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    TEXT_REDACTED_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    WORDS_FIELD_NUMBER: _ClassVar[int]
    turn_index: int
    speaker_label: SpeakerRole
    start_ms: int
    end_ms: int
    text: str
    text_redacted: str
    confidence: float
    words: _containers.RepeatedCompositeFieldContainer[Word]
    def __init__(self, turn_index: _Optional[int] = ..., speaker_label: _Optional[_Union[SpeakerRole, str]] = ..., start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ..., text: _Optional[str] = ..., text_redacted: _Optional[str] = ..., confidence: _Optional[float] = ..., words: _Optional[_Iterable[_Union[Word, _Mapping]]] = ...) -> None: ...

class Transcript(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "language", "asr_backend", "asr_model", "turns", "speaker_clusters", "wer", "der")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    ASR_BACKEND_FIELD_NUMBER: _ClassVar[int]
    ASR_MODEL_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_CLUSTERS_FIELD_NUMBER: _ClassVar[int]
    WER_FIELD_NUMBER: _ClassVar[int]
    DER_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    language: str
    asr_backend: str
    asr_model: str
    turns: _containers.RepeatedCompositeFieldContainer[Turn]
    speaker_clusters: _containers.RepeatedCompositeFieldContainer[SpeakerCluster]
    wer: float
    der: float
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., language: _Optional[str] = ..., asr_backend: _Optional[str] = ..., asr_model: _Optional[str] = ..., turns: _Optional[_Iterable[_Union[Turn, _Mapping]]] = ..., speaker_clusters: _Optional[_Iterable[_Union[SpeakerCluster, _Mapping]]] = ..., wer: _Optional[float] = ..., der: _Optional[float] = ...) -> None: ...
