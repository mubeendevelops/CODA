from coda.v1 import clinical_pb2 as _clinical_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Candidate(_message.Message):
    __slots__ = ("index", "field_key", "text", "source_thought_ids", "generated_by_model", "variant", "is_refinement", "items")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THOUGHT_IDS_FIELD_NUMBER: _ClassVar[int]
    GENERATED_BY_MODEL_FIELD_NUMBER: _ClassVar[int]
    VARIANT_FIELD_NUMBER: _ClassVar[int]
    IS_REFINEMENT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    index: int
    field_key: _clinical_pb2.FieldKey
    text: str
    source_thought_ids: _containers.RepeatedScalarFieldContainer[str]
    generated_by_model: str
    variant: str
    is_refinement: bool
    items: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, index: _Optional[int] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., text: _Optional[str] = ..., source_thought_ids: _Optional[_Iterable[str]] = ..., generated_by_model: _Optional[str] = ..., variant: _Optional[str] = ..., is_refinement: bool = ..., items: _Optional[_Iterable[str]] = ...) -> None: ...

class ScoreBreakdown(_message.Message):
    __slots__ = ("relevance", "consistency", "redundancy", "aggregate", "judge_model", "scorer_backend", "consistency_backend", "contradiction_penalty", "judge_rationale")
    RELEVANCE_FIELD_NUMBER: _ClassVar[int]
    CONSISTENCY_FIELD_NUMBER: _ClassVar[int]
    REDUNDANCY_FIELD_NUMBER: _ClassVar[int]
    AGGREGATE_FIELD_NUMBER: _ClassVar[int]
    JUDGE_MODEL_FIELD_NUMBER: _ClassVar[int]
    SCORER_BACKEND_FIELD_NUMBER: _ClassVar[int]
    CONSISTENCY_BACKEND_FIELD_NUMBER: _ClassVar[int]
    CONTRADICTION_PENALTY_FIELD_NUMBER: _ClassVar[int]
    JUDGE_RATIONALE_FIELD_NUMBER: _ClassVar[int]
    relevance: float
    consistency: float
    redundancy: float
    aggregate: float
    judge_model: str
    scorer_backend: str
    consistency_backend: str
    contradiction_penalty: float
    judge_rationale: str
    def __init__(self, relevance: _Optional[float] = ..., consistency: _Optional[float] = ..., redundancy: _Optional[float] = ..., aggregate: _Optional[float] = ..., judge_model: _Optional[str] = ..., scorer_backend: _Optional[str] = ..., consistency_backend: _Optional[str] = ..., contradiction_penalty: _Optional[float] = ..., judge_rationale: _Optional[str] = ...) -> None: ...

class CandidateSet(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "field_key", "iteration", "candidates", "scores", "selected_index", "secondary_scores", "context_tokens")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    ITERATION_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    SCORES_FIELD_NUMBER: _ClassVar[int]
    SELECTED_INDEX_FIELD_NUMBER: _ClassVar[int]
    SECONDARY_SCORES_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    field_key: _clinical_pb2.FieldKey
    iteration: int
    candidates: _containers.RepeatedCompositeFieldContainer[Candidate]
    scores: _containers.RepeatedCompositeFieldContainer[ScoreBreakdown]
    selected_index: int
    secondary_scores: _containers.RepeatedCompositeFieldContainer[ScoreBreakdown]
    context_tokens: int
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., iteration: _Optional[int] = ..., candidates: _Optional[_Iterable[_Union[Candidate, _Mapping]]] = ..., scores: _Optional[_Iterable[_Union[ScoreBreakdown, _Mapping]]] = ..., selected_index: _Optional[int] = ..., secondary_scores: _Optional[_Iterable[_Union[ScoreBreakdown, _Mapping]]] = ..., context_tokens: _Optional[int] = ...) -> None: ...

class RefinementTrace(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "field_key", "iterations", "final_value", "score_trajectory", "tokens_in", "tokens_out", "llm_calls", "cache_hits", "scorer_backend")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    FIELD_KEY_FIELD_NUMBER: _ClassVar[int]
    ITERATIONS_FIELD_NUMBER: _ClassVar[int]
    FINAL_VALUE_FIELD_NUMBER: _ClassVar[int]
    SCORE_TRAJECTORY_FIELD_NUMBER: _ClassVar[int]
    TOKENS_IN_FIELD_NUMBER: _ClassVar[int]
    TOKENS_OUT_FIELD_NUMBER: _ClassVar[int]
    LLM_CALLS_FIELD_NUMBER: _ClassVar[int]
    CACHE_HITS_FIELD_NUMBER: _ClassVar[int]
    SCORER_BACKEND_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    field_key: _clinical_pb2.FieldKey
    iterations: _containers.RepeatedCompositeFieldContainer[CandidateSet]
    final_value: _clinical_pb2.FieldValue
    score_trajectory: _containers.RepeatedScalarFieldContainer[float]
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int
    scorer_backend: str
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., field_key: _Optional[_Union[_clinical_pb2.FieldKey, str]] = ..., iterations: _Optional[_Iterable[_Union[CandidateSet, _Mapping]]] = ..., final_value: _Optional[_Union[_clinical_pb2.FieldValue, _Mapping]] = ..., score_trajectory: _Optional[_Iterable[float]] = ..., tokens_in: _Optional[int] = ..., tokens_out: _Optional[int] = ..., llm_calls: _Optional[int] = ..., cache_hits: _Optional[int] = ..., scorer_backend: _Optional[str] = ...) -> None: ...
