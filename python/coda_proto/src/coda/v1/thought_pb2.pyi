from coda.v1 import transcript_pb2 as _transcript_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ThoughtCategory(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    THOUGHT_CATEGORY_UNSPECIFIED: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_SYMPTOM: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_HISTORY: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_MEDICATION: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_ALLERGY: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_EXAMINATION: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_DIAGNOSIS: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_INVESTIGATION: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_PLAN: _ClassVar[ThoughtCategory]
    THOUGHT_CATEGORY_OTHER: _ClassVar[ThoughtCategory]

class EdgeType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EDGE_TYPE_UNSPECIFIED: _ClassVar[EdgeType]
    EDGE_TYPE_TEMPORAL: _ClassVar[EdgeType]
    EDGE_TYPE_CAUSAL: _ClassVar[EdgeType]
    EDGE_TYPE_LOGICAL: _ClassVar[EdgeType]
    EDGE_TYPE_NEGATION: _ClassVar[EdgeType]
    EDGE_TYPE_ELABORATION: _ClassVar[EdgeType]
    EDGE_TYPE_COREFERENCE: _ClassVar[EdgeType]

class Polarity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    POLARITY_UNSPECIFIED: _ClassVar[Polarity]
    POLARITY_ASSERTED: _ClassVar[Polarity]
    POLARITY_NEGATED: _ClassVar[Polarity]
    POLARITY_UNCERTAIN: _ClassVar[Polarity]
    POLARITY_HYPOTHETICAL: _ClassVar[Polarity]

class PredictedBy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PREDICTED_BY_UNSPECIFIED: _ClassVar[PredictedBy]
    PREDICTED_BY_RULE: _ClassVar[PredictedBy]
    PREDICTED_BY_LLM: _ClassVar[PredictedBy]
THOUGHT_CATEGORY_UNSPECIFIED: ThoughtCategory
THOUGHT_CATEGORY_SYMPTOM: ThoughtCategory
THOUGHT_CATEGORY_HISTORY: ThoughtCategory
THOUGHT_CATEGORY_MEDICATION: ThoughtCategory
THOUGHT_CATEGORY_ALLERGY: ThoughtCategory
THOUGHT_CATEGORY_EXAMINATION: ThoughtCategory
THOUGHT_CATEGORY_DIAGNOSIS: ThoughtCategory
THOUGHT_CATEGORY_INVESTIGATION: ThoughtCategory
THOUGHT_CATEGORY_PLAN: ThoughtCategory
THOUGHT_CATEGORY_OTHER: ThoughtCategory
EDGE_TYPE_UNSPECIFIED: EdgeType
EDGE_TYPE_TEMPORAL: EdgeType
EDGE_TYPE_CAUSAL: EdgeType
EDGE_TYPE_LOGICAL: EdgeType
EDGE_TYPE_NEGATION: EdgeType
EDGE_TYPE_ELABORATION: EdgeType
EDGE_TYPE_COREFERENCE: EdgeType
POLARITY_UNSPECIFIED: Polarity
POLARITY_ASSERTED: Polarity
POLARITY_NEGATED: Polarity
POLARITY_UNCERTAIN: Polarity
POLARITY_HYPOTHETICAL: Polarity
PREDICTED_BY_UNSPECIFIED: PredictedBy
PREDICTED_BY_RULE: PredictedBy
PREDICTED_BY_LLM: PredictedBy

class LinkedEntity(_message.Message):
    __slots__ = ("text", "mesh_id", "icd10_code", "entity_type")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MESH_ID_FIELD_NUMBER: _ClassVar[int]
    ICD10_CODE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_TYPE_FIELD_NUMBER: _ClassVar[int]
    text: str
    mesh_id: str
    icd10_code: str
    entity_type: str
    def __init__(self, text: _Optional[str] = ..., mesh_id: _Optional[str] = ..., icd10_code: _Optional[str] = ..., entity_type: _Optional[str] = ...) -> None: ...

class Thought(_message.Message):
    __slots__ = ("id", "consultation_id", "run_config_id", "turn_id", "speaker", "text", "entities", "category", "temporal_anchor", "linked_concepts", "polarity", "confidence", "char_start", "char_end", "turn_index")
    ID_FIELD_NUMBER: _ClassVar[int]
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ENTITIES_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    TEMPORAL_ANCHOR_FIELD_NUMBER: _ClassVar[int]
    LINKED_CONCEPTS_FIELD_NUMBER: _ClassVar[int]
    POLARITY_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    CHAR_START_FIELD_NUMBER: _ClassVar[int]
    CHAR_END_FIELD_NUMBER: _ClassVar[int]
    TURN_INDEX_FIELD_NUMBER: _ClassVar[int]
    id: str
    consultation_id: str
    run_config_id: str
    turn_id: str
    speaker: _transcript_pb2.SpeakerRole
    text: str
    entities: _containers.RepeatedCompositeFieldContainer[LinkedEntity]
    category: ThoughtCategory
    temporal_anchor: str
    linked_concepts: _containers.RepeatedScalarFieldContainer[str]
    polarity: Polarity
    confidence: float
    char_start: int
    char_end: int
    turn_index: int
    def __init__(self, id: _Optional[str] = ..., consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., speaker: _Optional[_Union[_transcript_pb2.SpeakerRole, str]] = ..., text: _Optional[str] = ..., entities: _Optional[_Iterable[_Union[LinkedEntity, _Mapping]]] = ..., category: _Optional[_Union[ThoughtCategory, str]] = ..., temporal_anchor: _Optional[str] = ..., linked_concepts: _Optional[_Iterable[str]] = ..., polarity: _Optional[_Union[Polarity, str]] = ..., confidence: _Optional[float] = ..., char_start: _Optional[int] = ..., char_end: _Optional[int] = ..., turn_index: _Optional[int] = ...) -> None: ...

class ThoughtEdge(_message.Message):
    __slots__ = ("id", "consultation_id", "run_config_id", "src_thought_id", "dst_thought_id", "edge_type", "weight", "predicted_by", "rationale")
    ID_FIELD_NUMBER: _ClassVar[int]
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    SRC_THOUGHT_ID_FIELD_NUMBER: _ClassVar[int]
    DST_THOUGHT_ID_FIELD_NUMBER: _ClassVar[int]
    EDGE_TYPE_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    PREDICTED_BY_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    id: str
    consultation_id: str
    run_config_id: str
    src_thought_id: str
    dst_thought_id: str
    edge_type: EdgeType
    weight: float
    predicted_by: PredictedBy
    rationale: str
    def __init__(self, id: _Optional[str] = ..., consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., src_thought_id: _Optional[str] = ..., dst_thought_id: _Optional[str] = ..., edge_type: _Optional[_Union[EdgeType, str]] = ..., weight: _Optional[float] = ..., predicted_by: _Optional[_Union[PredictedBy, str]] = ..., rationale: _Optional[str] = ...) -> None: ...

class ThoughtGraph(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "thoughts", "edges", "prompt_set_hash", "language", "construction_model", "edge_model", "turn_count")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    THOUGHTS_FIELD_NUMBER: _ClassVar[int]
    EDGES_FIELD_NUMBER: _ClassVar[int]
    PROMPT_SET_HASH_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    CONSTRUCTION_MODEL_FIELD_NUMBER: _ClassVar[int]
    EDGE_MODEL_FIELD_NUMBER: _ClassVar[int]
    TURN_COUNT_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    thoughts: _containers.RepeatedCompositeFieldContainer[Thought]
    edges: _containers.RepeatedCompositeFieldContainer[ThoughtEdge]
    prompt_set_hash: str
    language: str
    construction_model: str
    edge_model: str
    turn_count: int
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., thoughts: _Optional[_Iterable[_Union[Thought, _Mapping]]] = ..., edges: _Optional[_Iterable[_Union[ThoughtEdge, _Mapping]]] = ..., prompt_set_hash: _Optional[str] = ..., language: _Optional[str] = ..., construction_model: _Optional[str] = ..., edge_model: _Optional[str] = ..., turn_count: _Optional[int] = ...) -> None: ...
