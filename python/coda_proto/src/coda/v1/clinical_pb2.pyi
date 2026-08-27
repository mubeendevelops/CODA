from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FieldKey(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FIELD_KEY_UNSPECIFIED: _ClassVar[FieldKey]
    FIELD_KEY_CHIEF_COMPLAINT: _ClassVar[FieldKey]
    FIELD_KEY_HOPI: _ClassVar[FieldKey]
    FIELD_KEY_PAST_MEDICAL_HISTORY: _ClassVar[FieldKey]
    FIELD_KEY_MEDICATIONS: _ClassVar[FieldKey]
    FIELD_KEY_ALLERGIES: _ClassVar[FieldKey]
    FIELD_KEY_EXAMINATION_FINDINGS: _ClassVar[FieldKey]
    FIELD_KEY_PROVISIONAL_DIAGNOSIS: _ClassVar[FieldKey]
    FIELD_KEY_INVESTIGATIONS_ADVISED: _ClassVar[FieldKey]
    FIELD_KEY_TREATMENT_PLAN: _ClassVar[FieldKey]

class NoteStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NOTE_STATUS_UNSPECIFIED: _ClassVar[NoteStatus]
    NOTE_STATUS_DRAFT: _ClassVar[NoteStatus]
    NOTE_STATUS_UNDER_REVIEW: _ClassVar[NoteStatus]
    NOTE_STATUS_APPROVED: _ClassVar[NoteStatus]
FIELD_KEY_UNSPECIFIED: FieldKey
FIELD_KEY_CHIEF_COMPLAINT: FieldKey
FIELD_KEY_HOPI: FieldKey
FIELD_KEY_PAST_MEDICAL_HISTORY: FieldKey
FIELD_KEY_MEDICATIONS: FieldKey
FIELD_KEY_ALLERGIES: FieldKey
FIELD_KEY_EXAMINATION_FINDINGS: FieldKey
FIELD_KEY_PROVISIONAL_DIAGNOSIS: FieldKey
FIELD_KEY_INVESTIGATIONS_ADVISED: FieldKey
FIELD_KEY_TREATMENT_PLAN: FieldKey
NOTE_STATUS_UNSPECIFIED: NoteStatus
NOTE_STATUS_DRAFT: NoteStatus
NOTE_STATUS_UNDER_REVIEW: NoteStatus
NOTE_STATUS_APPROVED: NoteStatus

class FieldValue(_message.Message):
    __slots__ = ("value", "source_turn_ids", "confidence", "linked_concepts")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TURN_IDS_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    LINKED_CONCEPTS_FIELD_NUMBER: _ClassVar[int]
    value: str
    source_turn_ids: _containers.RepeatedScalarFieldContainer[int]
    confidence: float
    linked_concepts: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, value: _Optional[str] = ..., source_turn_ids: _Optional[_Iterable[int]] = ..., confidence: _Optional[float] = ..., linked_concepts: _Optional[_Iterable[str]] = ...) -> None: ...

class MedicationsAllergies(_message.Message):
    __slots__ = ("medications", "allergies")
    MEDICATIONS_FIELD_NUMBER: _ClassVar[int]
    ALLERGIES_FIELD_NUMBER: _ClassVar[int]
    medications: _containers.RepeatedCompositeFieldContainer[FieldValue]
    allergies: _containers.RepeatedCompositeFieldContainer[FieldValue]
    def __init__(self, medications: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ..., allergies: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ...) -> None: ...

class ClinicalNote(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "version", "status", "chief_complaint", "hopi", "past_medical_history", "medications_allergies", "examination_findings", "provisional_diagnosis", "investigations_advised", "treatment_plan", "approved_by", "approved_at")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CHIEF_COMPLAINT_FIELD_NUMBER: _ClassVar[int]
    HOPI_FIELD_NUMBER: _ClassVar[int]
    PAST_MEDICAL_HISTORY_FIELD_NUMBER: _ClassVar[int]
    MEDICATIONS_ALLERGIES_FIELD_NUMBER: _ClassVar[int]
    EXAMINATION_FINDINGS_FIELD_NUMBER: _ClassVar[int]
    PROVISIONAL_DIAGNOSIS_FIELD_NUMBER: _ClassVar[int]
    INVESTIGATIONS_ADVISED_FIELD_NUMBER: _ClassVar[int]
    TREATMENT_PLAN_FIELD_NUMBER: _ClassVar[int]
    APPROVED_BY_FIELD_NUMBER: _ClassVar[int]
    APPROVED_AT_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    version: int
    status: NoteStatus
    chief_complaint: FieldValue
    hopi: FieldValue
    past_medical_history: _containers.RepeatedCompositeFieldContainer[FieldValue]
    medications_allergies: MedicationsAllergies
    examination_findings: FieldValue
    provisional_diagnosis: _containers.RepeatedCompositeFieldContainer[FieldValue]
    investigations_advised: _containers.RepeatedCompositeFieldContainer[FieldValue]
    treatment_plan: FieldValue
    approved_by: str
    approved_at: str
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., version: _Optional[int] = ..., status: _Optional[_Union[NoteStatus, str]] = ..., chief_complaint: _Optional[_Union[FieldValue, _Mapping]] = ..., hopi: _Optional[_Union[FieldValue, _Mapping]] = ..., past_medical_history: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ..., medications_allergies: _Optional[_Union[MedicationsAllergies, _Mapping]] = ..., examination_findings: _Optional[_Union[FieldValue, _Mapping]] = ..., provisional_diagnosis: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ..., investigations_advised: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ..., treatment_plan: _Optional[_Union[FieldValue, _Mapping]] = ..., approved_by: _Optional[str] = ..., approved_at: _Optional[str] = ...) -> None: ...

class Summary(_message.Message):
    __slots__ = ("consultation_id", "run_config_id", "text", "rouge_l", "bertscore")
    CONSULTATION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ROUGE_L_FIELD_NUMBER: _ClassVar[int]
    BERTSCORE_FIELD_NUMBER: _ClassVar[int]
    consultation_id: str
    run_config_id: str
    text: str
    rouge_l: float
    bertscore: float
    def __init__(self, consultation_id: _Optional[str] = ..., run_config_id: _Optional[str] = ..., text: _Optional[str] = ..., rouge_l: _Optional[float] = ..., bertscore: _Optional[float] = ...) -> None: ...
