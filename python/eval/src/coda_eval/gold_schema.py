"""JSON Schema and validator for gold (human-annotated) reference files —
plan.md Phase 5's "gold annotation format for the 8 fields plus gold speaker
labels". One file per consultation item, at
`data/gold/<dataset>/<session_id>.gold.json`.

Deliberately its own schema, not a reuse of nlp_service's EXTRACTION_SCHEMA
(python/nlp-service/src/nlp_service/schema.py): a gold file additionally
carries per-turn speaker ground truth and annotation provenance metadata that
a model's output never does, and drops `confidence` (a human annotation
either records a value or it doesn't — there is no partial-confidence
gold). The field shape (`value` + `source_turn_ids`) is kept identical on
purpose so the same field-matching code (metrics/field_match.py,
metrics/field_scoring.py) scores a gold FieldValue against a hypothesis
FieldValue without a shape-translation step.

See docs/eval/annotation_guide.md for the human-facing annotation
instructions this schema encodes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema

GOLD_SCHEMA_VERSION = 1

_GOLD_FIELD_VALUE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "source_turn_ids"],
    "properties": {
        "value": {"type": "string", "minLength": 1},
        "source_turn_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "integer", "minimum": 0},
        },
    },
}
_NULLABLE_GOLD_FIELD_VALUE = {"anyOf": [_GOLD_FIELD_VALUE_SCHEMA, {"type": "null"}]}
_GOLD_FIELD_VALUE_LIST = {"type": "array", "items": _GOLD_FIELD_VALUE_SCHEMA}

_SCALAR_FIELDS = ("chief_complaint", "hopi", "examination_findings", "treatment_plan")
_LIST_FIELDS = (
    "past_medical_history",
    "medications",
    "allergies",
    "provisional_diagnosis",
    "investigations_advised",
)

_GOLD_FIELDS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*_SCALAR_FIELDS, *_LIST_FIELDS],
    "properties": {
        "chief_complaint": _NULLABLE_GOLD_FIELD_VALUE,
        "hopi": _NULLABLE_GOLD_FIELD_VALUE,
        "examination_findings": _NULLABLE_GOLD_FIELD_VALUE,
        "treatment_plan": _NULLABLE_GOLD_FIELD_VALUE,
        "past_medical_history": _GOLD_FIELD_VALUE_LIST,
        "medications": _GOLD_FIELD_VALUE_LIST,
        "allergies": _GOLD_FIELD_VALUE_LIST,
        "provisional_diagnosis": _GOLD_FIELD_VALUE_LIST,
        "investigations_advised": _GOLD_FIELD_VALUE_LIST,
    },
}

_SPEAKER_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["turn_id", "speaker"],
    "properties": {
        "turn_id": {"type": "integer", "minimum": 0},
        # "Unknown" is a legal gold value: some PriMock57 turns are genuinely
        # ambiguous (crosstalk, inaudible) even against the ground-truth
        # per-channel audio, and the annotation guide says to record that
        # honestly rather than force a guess.
        "speaker": {"type": "string", "enum": ["Doctor", "Patient", "Unknown"]},
    },
}

GOLD_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "item_id",
        "dataset",
        "session_id",
        "language",
        "annotator",
        "annotated_at",
        "n_turns",
        "speakers",
        "fields",
        "summary",
    ],
    "properties": {
        "schema_version": {"const": GOLD_SCHEMA_VERSION},
        "item_id": {"type": "string", "minLength": 1},
        "dataset": {"type": "string", "minLength": 1},
        "session_id": {"type": "string", "minLength": 1},
        # v1 is English-only (claude_context.md §2.1) but the field stays a
        # plain string, never a hard-coded "en" enum, so a v2 kn_en gold file
        # needs no schema change.
        "language": {"type": "string", "minLength": 1},
        "annotator": {"type": "string", "minLength": 1},
        "annotated_at": {"type": "string", "minLength": 1},
        "n_turns": {"type": "integer", "minimum": 1},
        "speakers": {"type": "array", "items": _SPEAKER_SCHEMA},
        "fields": _GOLD_FIELDS_SCHEMA,
        "summary": {"type": "string", "minLength": 1},
    },
}


@dataclass(frozen=True, slots=True)
class GoldValidationResult:
    valid: bool
    errors: list[str]
    parsed: dict[str, object] | None


def validate_gold_dict(data: object) -> GoldValidationResult:
    """Runs schema validation, then the semantic checks jsonschema can't
    express: every speaker's turn_id is unique and covers exactly
    `range(n_turns)`, and every field's `source_turn_ids` reference a turn_id
    that actually appears in `speakers`. Stops at the first failure category
    but collects all errors within that category, since annotation errors
    tend to cluster (fix the turn numbering once, not error-by-error).
    """
    try:
        jsonschema.validate(instance=data, schema=GOLD_SCHEMA)
    except jsonschema.ValidationError as exc:
        return GoldValidationResult(
            valid=False,
            errors=[f"schema validation failed at {list(exc.absolute_path)}: {exc.message}"],
            parsed=None,
        )

    assert isinstance(data, dict)
    errors = _validate_speaker_coverage(data)
    if errors:
        return GoldValidationResult(valid=False, errors=errors, parsed=None)

    errors = _validate_field_turn_refs(data)
    if errors:
        return GoldValidationResult(valid=False, errors=errors, parsed=None)

    return GoldValidationResult(valid=True, errors=[], parsed=data)


def _validate_speaker_coverage(data: dict[str, object]) -> list[str]:
    n_turns = data["n_turns"]
    assert isinstance(n_turns, int)
    speakers = data["speakers"]
    assert isinstance(speakers, list)

    seen: dict[int, str] = {}
    errors = []
    for entry in speakers:
        assert isinstance(entry, dict)
        turn_id = entry["turn_id"]
        assert isinstance(turn_id, int)
        if turn_id in seen:
            errors.append(f"speakers: turn_id {turn_id} listed more than once")
        seen[turn_id] = str(entry["speaker"])

    expected = set(range(n_turns))
    missing = expected - seen.keys()
    extra = seen.keys() - expected
    if missing:
        errors.append(f"speakers: missing turn_id(s) {sorted(missing)} (n_turns={n_turns})")
    if extra:
        errors.append(f"speakers: turn_id(s) {sorted(extra)} exceed n_turns={n_turns}")
    return errors


def _validate_field_turn_refs(data: dict[str, object]) -> list[str]:
    n_turns = data["n_turns"]
    assert isinstance(n_turns, int)
    known = set(range(n_turns))
    fields = data["fields"]
    assert isinstance(fields, dict)

    errors = []
    for field_name in _SCALAR_FIELDS:
        value = fields.get(field_name)
        if value is None:
            continue
        assert isinstance(value, dict)
        bad = _unknown_ids(value, known)
        if bad:
            errors.append(
                f"fields.{field_name}.source_turn_ids references unknown turn(s) {sorted(bad)}"
            )

    for field_name in _LIST_FIELDS:
        items = fields.get(field_name) or []
        assert isinstance(items, list)
        for idx, item in enumerate(items):
            assert isinstance(item, dict)
            bad = _unknown_ids(item, known)
            if bad:
                errors.append(
                    f"fields.{field_name}[{idx}].source_turn_ids references unknown turn(s) "
                    f"{sorted(bad)}"
                )
    return errors


def _unknown_ids(field_value: dict[str, object], known_turn_ids: set[int]) -> set[int]:
    ids = field_value.get("source_turn_ids") or []
    assert isinstance(ids, list)
    return {i for i in ids if i not in known_turn_ids}


def validate_gold_file(path: Path) -> GoldValidationResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return GoldValidationResult(valid=False, errors=[f"not valid JSON: {exc}"], parsed=None)
    return validate_gold_dict(data)


__all__ = [
    "GOLD_SCHEMA",
    "GOLD_SCHEMA_VERSION",
    "GoldValidationResult",
    "validate_gold_dict",
    "validate_gold_file",
]
