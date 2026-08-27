from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ScorerWeights(_message.Message):
    __slots__ = ("relevance", "consistency", "redundancy")
    RELEVANCE_FIELD_NUMBER: _ClassVar[int]
    CONSISTENCY_FIELD_NUMBER: _ClassVar[int]
    REDUNDANCY_FIELD_NUMBER: _ClassVar[int]
    relevance: float
    consistency: float
    redundancy: float
    def __init__(self, relevance: _Optional[float] = ..., consistency: _Optional[float] = ..., redundancy: _Optional[float] = ...) -> None: ...

class RunConfig(_message.Message):
    __slots__ = ("arm", "schema_version", "base_model", "judge_model", "structural_model", "embed_model", "asr_backend", "asr_model", "got_enabled", "n_candidates", "k_iterations", "graph_context_enabled", "kg_enabled", "kg_backend", "scorer_weights", "temperature", "top_p", "seed", "prompt_set_hash", "redaction_enabled")
    ARM_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    BASE_MODEL_FIELD_NUMBER: _ClassVar[int]
    JUDGE_MODEL_FIELD_NUMBER: _ClassVar[int]
    STRUCTURAL_MODEL_FIELD_NUMBER: _ClassVar[int]
    EMBED_MODEL_FIELD_NUMBER: _ClassVar[int]
    ASR_BACKEND_FIELD_NUMBER: _ClassVar[int]
    ASR_MODEL_FIELD_NUMBER: _ClassVar[int]
    GOT_ENABLED_FIELD_NUMBER: _ClassVar[int]
    N_CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    K_ITERATIONS_FIELD_NUMBER: _ClassVar[int]
    GRAPH_CONTEXT_ENABLED_FIELD_NUMBER: _ClassVar[int]
    KG_ENABLED_FIELD_NUMBER: _ClassVar[int]
    KG_BACKEND_FIELD_NUMBER: _ClassVar[int]
    SCORER_WEIGHTS_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    TOP_P_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    PROMPT_SET_HASH_FIELD_NUMBER: _ClassVar[int]
    REDACTION_ENABLED_FIELD_NUMBER: _ClassVar[int]
    arm: str
    schema_version: int
    base_model: str
    judge_model: str
    structural_model: str
    embed_model: str
    asr_backend: str
    asr_model: str
    got_enabled: bool
    n_candidates: int
    k_iterations: int
    graph_context_enabled: bool
    kg_enabled: bool
    kg_backend: str
    scorer_weights: ScorerWeights
    temperature: float
    top_p: float
    seed: int
    prompt_set_hash: str
    redaction_enabled: bool
    def __init__(self, arm: _Optional[str] = ..., schema_version: _Optional[int] = ..., base_model: _Optional[str] = ..., judge_model: _Optional[str] = ..., structural_model: _Optional[str] = ..., embed_model: _Optional[str] = ..., asr_backend: _Optional[str] = ..., asr_model: _Optional[str] = ..., got_enabled: bool = ..., n_candidates: _Optional[int] = ..., k_iterations: _Optional[int] = ..., graph_context_enabled: bool = ..., kg_enabled: bool = ..., kg_backend: _Optional[str] = ..., scorer_weights: _Optional[_Union[ScorerWeights, _Mapping]] = ..., temperature: _Optional[float] = ..., top_p: _Optional[float] = ..., seed: _Optional[int] = ..., prompt_set_hash: _Optional[str] = ..., redaction_enabled: bool = ...) -> None: ...
