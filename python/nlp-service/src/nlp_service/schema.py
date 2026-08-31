"""JSON Schema for the single-pass extraction output, plus the three-layer
validation the repair loop (extraction.py) drives: (a) syntactic — valid
JSON, (b) schema — matches this shape, (c) semantic — every
`source_turn_ids` entry references a turn that actually exists in the
transcript being processed. Mirrors clinical.proto's FieldValue/
MedicationsAllergies/ClinicalNote shape (claude_context.md §3's 8 fields);
kept as a plain JSON Schema (not generated from the .proto) since the two
have different jobs — the proto is the persisted/transport shape, this is
what the repair loop can hand the model verbatim as a validation error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import jsonschema

FIELD_VALUE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "source_turn_ids", "confidence"],
    "properties": {
        "value": {"type": "string"},
        "source_turn_ids": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

_NULLABLE_FIELD_VALUE = {"anyOf": [FIELD_VALUE_SCHEMA, {"type": "null"}]}
_FIELD_VALUE_LIST = {"type": "array", "items": FIELD_VALUE_SCHEMA}

EXTRACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "chief_complaint",
        "hopi",
        "past_medical_history",
        "medications",
        "allergies",
        "examination_findings",
        "provisional_diagnosis",
        "investigations_advised",
        "treatment_plan",
    ],
    "properties": {
        "chief_complaint": _NULLABLE_FIELD_VALUE,
        "hopi": _NULLABLE_FIELD_VALUE,
        "past_medical_history": _FIELD_VALUE_LIST,
        "medications": _FIELD_VALUE_LIST,
        "allergies": _FIELD_VALUE_LIST,
        "examination_findings": _NULLABLE_FIELD_VALUE,
        "provisional_diagnosis": _FIELD_VALUE_LIST,
        "investigations_advised": _FIELD_VALUE_LIST,
        "treatment_plan": _NULLABLE_FIELD_VALUE,
    },
}

_SCALAR_FIELDS = ("chief_complaint", "hopi", "examination_findings", "treatment_plan")
_LIST_FIELDS = (
    "past_medical_history",
    "medications",
    "allergies",
    "provisional_diagnosis",
    "investigations_advised",
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    error: str
    """Human-readable, model-facing description of the first failure found.
    Empty when valid."""
    parsed: dict[str, object] | None
    """The parsed extraction dict, present only when valid=True."""


def validate_extraction(raw_content: str, *, known_turn_ids: set[int]) -> ValidationResult:
    """Runs all three validation layers in order, stopping at the first
    failure (the repair loop only needs one error to hand back at a time).
    """
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        return ValidationResult(
            valid=False, error=f"response is not valid JSON: {exc}", parsed=None
        )

    try:
        jsonschema.validate(instance=data, schema=EXTRACTION_SCHEMA)
    except jsonschema.ValidationError as exc:
        return ValidationResult(
            valid=False,
            error=f"schema validation failed at {list(exc.absolute_path)}: {exc.message}",
            parsed=None,
        )

    assert isinstance(data, dict)
    semantic_error = _validate_turn_ids(data, known_turn_ids)
    if semantic_error:
        return ValidationResult(valid=False, error=semantic_error, parsed=None)

    return ValidationResult(valid=True, error="", parsed=data)


def _validate_turn_ids(data: dict[str, object], known_turn_ids: set[int]) -> str:
    for field_name in _SCALAR_FIELDS:
        value = data.get(field_name)
        if value is None:
            continue
        assert isinstance(value, dict)
        bad = _unknown_ids(value, known_turn_ids)
        if bad:
            return (
                f"{field_name}.source_turn_ids references turn(s) {sorted(bad)} "
                "that do not exist in the transcript"
            )

    for field_name in _LIST_FIELDS:
        items = data.get(field_name) or []
        assert isinstance(items, list)
        for idx, item in enumerate(items):
            assert isinstance(item, dict)
            bad = _unknown_ids(item, known_turn_ids)
            if bad:
                return (
                    f"{field_name}[{idx}].source_turn_ids references turn(s) {sorted(bad)} "
                    "that do not exist in the transcript"
                )

    return ""


def _unknown_ids(field_value: dict[str, object], known_turn_ids: set[int]) -> set[int]:
    ids = field_value.get("source_turn_ids") or []
    assert isinstance(ids, list)
    return {i for i in ids if i not in known_turn_ids}


__all__ = [
    "FIELD_VALUE_SCHEMA",
    "EXTRACTION_SCHEMA",
    "ValidationResult",
    "validate_extraction",
]
