"""Validation for Module 5/6's LLM calls, reusing the three-layer discipline
`nlp_service.schema` and `nlp_service.graph.schema` already apply: syntactic,
schema, then semantic.

The semantic layer here is about **provenance**, as it was for thought
construction: a generated candidate must cite the thought ids it was built
from, and those ids must be ones the prompt actually supplied. A candidate
citing an invented thought id is a candidate whose evidence chain cannot be
checked, which is the same failure as an unsupported value in the note —
claude_context.md §3 scores it as hallucination.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import jsonschema

from nlp_service.graph.schema import parse_json_object

CANDIDATE_FIELD_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "items", "source_thought_ids", "confidence"],
    "properties": {
        # Scalar fields use `value`; list fields use `items`. Both are always
        # present so the model never has to choose a shape, and exactly one
        # carries content — enforced semantically below, since JSON Schema's
        # oneOf produces error messages a repair prompt cannot act on.
        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "items": {"type": "array", "items": {"type": "string"}},
        # maxItems=6 is an output-size guard, not a modelling choice: real
        # thought ids are full database UUIDs (~12-15 tokens each), and a
        # field retrieving many supporting thoughts (found live 2026-09-05 —
        # one field cited 19 of them) can cite its way past this model's
        # real OTPM output-tokens-per-minute ceiling on its own, rejected
        # outright with no content returned at all. The system prompt asks
        # for "at most 6, the most directly relevant"; this is the hard
        # backstop when a response ignores that and needs to be repaired.
        "source_thought_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

GENERATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fields"],
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": CANDIDATE_FIELD_SCHEMA,
        }
    },
}

JUDGE_SCORE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["index", "relevance", "consistency", "redundancy", "rationale"],
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "relevance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "consistency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "redundancy": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string"},
    },
}

JUDGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scores"],
    "properties": {"scores": {"type": "array", "items": JUDGE_SCORE_SCHEMA}},
}

CRITIQUE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["critique", "revised"],
    "properties": {
        "critique": {"type": "string"},
        "revised": CANDIDATE_FIELD_SCHEMA,
    },
}


def str_list(payload: dict[str, object], key: str) -> list[str]:
    """`payload[key]` as a list of strings, or `[]` when absent or null.

    Generation and refinement pull the same three keys out of the same
    validated-but-untyped payload shape. Doing it here once, typed, keeps
    `# type: ignore` comments out of both call sites — the ignores were
    hiding the fact that nothing checked the value really was a list.
    """
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def scalar_text(payload: dict[str, object]) -> str:
    """`payload["value"]` as a string, or `""` for null/absent/empty."""
    value = payload.get("value")
    return str(value) if value else ""


def as_float(payload: dict[str, object], key: str) -> float:
    """`payload[key]` as a float, or 0.0 when absent or not a number."""
    raw = payload.get(key)
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    error: str
    items: list[dict[str, object]] = field(default_factory=list)
    parsed: dict[str, object] | None = None


def _check_field_payload(
    payload: dict[str, object], *, where: str, known_thought_ids: set[str], list_field: bool
) -> str:
    """Returns an empty string when valid, else a model-facing error."""
    value = payload.get("value")
    items = payload.get("items") or []
    assert isinstance(items, list)

    if list_field:
        if value not in (None, ""):
            return (
                f"{where} is a list-valued field, so `value` must be null and the "
                f"content must go in `items`. You put {value!r} in `value`."
            )
    elif items:
        return (
            f"{where} is a scalar field, so `items` must be empty and the content "
            f"must go in `value`. You put {len(items)} entries in `items`."
        )

    has_content = bool(items) if list_field else bool(value)
    ids = payload.get("source_thought_ids") or []
    assert isinstance(ids, list)

    if has_content and not ids:
        return (
            f"{where} has content but cites no source_thought_ids. Every non-empty "
            f"value must cite the thought ids it was built from."
        )
    if not has_content and ids:
        return (
            f"{where} is empty but cites source_thought_ids. An empty field must "
            f"cite nothing."
        )
    for tid in ids:
        if str(tid) not in known_thought_ids:
            return (
                f"{where} cites thought id {tid!r}, which was not in the supporting "
                f"thoughts you were given. Use only the ids listed for that field."
            )
    return ""


def validate_generation(
    raw_content: str, *, known_thought_ids: dict[str, set[str]], list_fields: set[str]
) -> ValidationResult:
    """`known_thought_ids` maps field_key to the thought ids that field's
    prompt block actually supplied. A candidate may only cite from its own
    field's block — citing another field's evidence would make the per-field
    subgraph retrieval meaningless.
    """
    parsed, err = parse_json_object(raw_content)
    if parsed is None:
        return ValidationResult(valid=False, error=err)
    try:
        jsonschema.validate(parsed, GENERATION_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ValidationResult(valid=False, error=f"schema violation at {path}: {exc.message}")

    fields = parsed["fields"]
    assert isinstance(fields, dict)
    expected = set(known_thought_ids)
    got = set(fields)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        parts = []
        if missing:
            parts.append(f"missing field(s): {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected field(s): {', '.join(extra)}")
        return ValidationResult(
            valid=False,
            error=(
                "the `fields` object must contain exactly the requested field keys — "
                + "; ".join(parts)
            ),
        )

    for field_key, payload in fields.items():
        assert isinstance(payload, dict)
        error = _check_field_payload(
            payload,
            where=f"fields.{field_key}",
            known_thought_ids=known_thought_ids[field_key],
            list_field=field_key in list_fields,
        )
        if error:
            return ValidationResult(valid=False, error=error)

    return ValidationResult(valid=True, error="", parsed=parsed)


def validate_judge_scores(raw_content: str, *, n_candidates: int) -> ValidationResult:
    parsed, err = parse_json_object(raw_content)
    if parsed is None:
        return ValidationResult(valid=False, error=err)
    try:
        jsonschema.validate(parsed, JUDGE_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ValidationResult(valid=False, error=f"schema violation at {path}: {exc.message}")

    scores = parsed["scores"]
    assert isinstance(scores, list)
    by_index: dict[int, dict[str, object]] = {}
    for entry in scores:
        assert isinstance(entry, dict)
        idx = int(as_float(entry, "index"))
        if idx >= n_candidates:
            return ValidationResult(
                valid=False,
                error=(
                    f"scores[].index = {idx} but only {n_candidates} candidate(s) were "
                    f"given (valid indices 0..{n_candidates - 1})"
                ),
            )
        by_index[idx] = entry
    if len(by_index) != n_candidates:
        return ValidationResult(
            valid=False,
            error=(
                f"expected exactly one score per candidate ({n_candidates} of them), "
                f"got {len(by_index)} distinct index values"
            ),
        )
    return ValidationResult(valid=True, error="", items=[by_index[i] for i in range(n_candidates)])


def validate_critique(
    raw_content: str, *, known_thought_ids: set[str], list_field: bool, field_key: str
) -> ValidationResult:
    parsed, err = parse_json_object(raw_content)
    if parsed is None:
        return ValidationResult(valid=False, error=err)
    try:
        jsonschema.validate(parsed, CRITIQUE_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ValidationResult(valid=False, error=f"schema violation at {path}: {exc.message}")

    revised = parsed["revised"]
    assert isinstance(revised, dict)
    error = _check_field_payload(
        revised,
        where=f"revised ({field_key})",
        known_thought_ids=known_thought_ids,
        list_field=list_field,
    )
    if error:
        return ValidationResult(valid=False, error=error)
    return ValidationResult(valid=True, error="", parsed=parsed)


__all__ = [
    "GENERATION_SCHEMA",
    "str_list",
    "scalar_text",
    "as_float",
    "JUDGE_SCHEMA",
    "CRITIQUE_SCHEMA",
    "ValidationResult",
    "validate_generation",
    "validate_judge_scores",
    "validate_critique",
]
