from coda.v1 import clinical_pb2 as _clinical_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Candidate(_message.Message):
    __slots__ = ("index", "field_key", "text", "source_thought_ids", "generated_by_model")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THOUGHT_IDS_FIELD_NUMBER: _ClassVar[int]
    GENERATED_BY_MODEL_FIELD_NUMBER: _ClassVar[int]
    index: int
    field_key: _clinical_pb2.FieldKey
    text: str
    source_thought_ids: _containers.RepeatedScalarFieldContainer[str]
    generated_by_model: str
    def __init__(self, index: _Optional[int] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., text: _Optional[str] = ..., source_thought_ids: _Optional[_Iterable[str]] = ..., generated_by_model: _Optional[str] = ...) -> None: ...

class ScoreBreakdown(_message.Message):
    __slots__ = ("relevance", "consistency", "redundancy", "aggregate", "judge_model")
    RELEVANCE_FIELD_NUMBER: _ClassVar[int]
    CONSISTENCY_FIELD_NUMBER: _ClassVar[int]
    REDUNDANCY_FIELD_NUMBER: _ClassVar[int]
    AGGREGATE_FIELD_NUMBER: _ClassVar[int]
    JUDGE_MODEL_FIELD_NUMBER: _ClassVar[int]
    relevance: float
    consistency: float
    redundancy: float
    aggregate: float
    judge_model: str
    def __init__(self, relevance: _Optional[float] = ..., consistency: _Optional[float] = ..., redundancy: _Optional[float] = ..., aggregate: _Optional[float] = ..., judge_model: _Optional[str] = ...) -> None: ...

class CandidateSet(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "field_key", "iteration", "candidates", "scores", "selected_index")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    ITERATION_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    SCORES_FIELD_NUMBER: _ClassVar[int]
    SELECTED_INDEX_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    field_key: _clinical_pb2.FieldKey
    iteration: int
    candidates: _containers.RepeatedCompositeFieldContainer[Candidate]
    scores: _containers.RepeatedCompositeFieldContainer[ScoreBreakdown]
    selected_index: int
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., iteration: _Optional[int] = ..., candidates: _Optional[_Iterable[_Union[Candidate, _Mapping]]] = ..., scores: _Optional[_Iterable[_Union[ScoreBreakdown, _Mapping]]] = ..., selected_index: _Optional[int] = ...) -> None: ...

class RefinementTrace(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "field_key", "iterations", "final_value")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    ITERATIONS_FIELD_NUMBER: _ClassVar[int]
    FINAL_VALUE_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    field_key: _clinical_pb2.FieldKey
    iterations: _containers.RepeatedCompositeFieldContainer[CandidateSet]
    final_value: _clinical_pb2.FieldValue
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., iterations: _Optional[_Iterable[_Union[CandidateSet, _Mapping]]] = ..., final_value: _Optional[_Union[_clinical_pb2.FieldValue, _Mapping]] = ...) -> None: ...
