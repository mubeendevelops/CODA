"""JSON Schema plus semantic validation for the two Module 1/2 LLM calls,
driving the same three-layer discipline `nlp_service.schema` applies to
single-pass extraction: (a) syntactic — valid JSON, (b) schema — matches the
declared shape, (c) semantic — the claims reference things that actually
exist.

The semantic layer is where most real failures land, and it is stricter here
than for extraction because thought construction makes a provenance claim
extraction does not: `text_span` must be a **verbatim substring of the cited
turn's text**. That single check is what keeps a thought traceable to the
exact words that produced it, and it is not enforceable by JSON Schema — a
model that paraphrases the span, silently strips a disfluency from it, or
cites the wrong turn produces a structurally perfect object that is
nonetheless unusable as evidence. Rejecting it into the repair loop is
cheaper than discovering it in the report figures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import jsonschema

CLINICAL_CATEGORIES = (
    "symptom",
    "history",
    "medication",
    "allergy",
    "examination",
    "diagnosis",
    "investigation",
    "plan",
    "other",
)
"""Mirrors coda.v1.ThoughtCategory and migration 000017's category CHECK."""

POLARITIES = ("asserted", "negated", "uncertain", "hypothetical")
"""Mirrors coda.v1.Polarity and migration 000032's polarity CHECK."""

LLM_EDGE_TYPES = ("causal", "logical", "negation", "elaboration", "coreference")
"""The five types the model predicts. `temporal` is deliberately absent: it
is rule-derived from turn order (edges.py) and a model emitting one is a
validation failure, not a free extra edge — accepting model-authored
temporal edges would silently mix a rule signal with a model signal in the
same field and make the "temporal edges are free in dialogue" claim
unfalsifiable."""

THOUGHT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "turn_index",
        "text_span",
        "text",
        "entities",
        "clinical_category",
        "temporal_anchor",
        "polarity",
        "confidence",
    ],
    "properties": {
        "turn_index": {"type": "integer", "minimum": 0},
        "text_span": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1},
        "entities": {"type": "array", "items": {"type": "string"}},
        "clinical_category": {"type": "string", "enum": list(CLINICAL_CATEGORIES)},
        "temporal_anchor": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "polarity": {"type": "string", "enum": list(POLARITIES)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

THOUGHT_CONSTRUCTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thoughts"],
    "properties": {"thoughts": {"type": "array", "items": THOUGHT_SCHEMA}},
}

EDGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["src_thought_id", "dst_thought_id", "edge_type", "confidence", "rationale"],
    "properties": {
        "src_thought_id": {"type": "string", "minLength": 1},
        "dst_thought_id": {"type": "string", "minLength": 1},
        "edge_type": {"type": "string", "enum": list(LLM_EDGE_TYPES)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string"},
    },
}

EDGE_PREDICTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edges"],
    "properties": {"edges": {"type": "array", "items": EDGE_SCHEMA}},
}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    error: str
    """Model-facing description of the first failure found. Empty when
    valid. Handed verbatim to the repair addendum, so it has to name the
    specific problem, not just "invalid"."""
    items: list[dict[str, object]] = field(default_factory=list)
    """The validated `thoughts` / `edges` list, present only when valid."""


def parse_json_object(raw_content: str) -> tuple[dict[str, object] | None, str]:
    """Tolerates a markdown code fence around the object — models wrap JSON
    in ```json despite instruction often enough that failing the whole call
    over it would spend a repair attempt on nothing.
    """
    text = raw_content.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
        text = text.strip()
    if not text:
        return None, "response was empty"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"response is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, f"response is a JSON {type(parsed).__name__}, expected an object"
    return parsed, ""


def _normalize(text: str) -> str:
    """Collapses whitespace for the substring check. A model that copies a
    span correctly but re-wraps a line, or turns a double space into a
    single one, has not actually broken provenance — failing that into the
    repair loop would burn attempts on a formatting artifact of how the
    transcript was rendered into the prompt.
    """
    return " ".join(text.split())


def locate_span(turn_text: str, span: str) -> tuple[int, int] | None:
    """Returns `[char_start, char_end)` of `span` within `turn_text`, or None
    if the span is not present.

    Falls back to a whitespace-normalized search when the literal one fails,
    and maps the match back to offsets in the ORIGINAL string so the stored
    span always indexes the real turn text rather than a normalized copy
    that exists nowhere else in the system.
    """
    idx = turn_text.find(span)
    if idx != -1:
        return idx, idx + len(span)

    norm_span = _normalize(span)
    if not norm_span:
        return None

    # Walk the original string, building the normalized form while keeping a
    # map from normalized offset back to original offset.
    norm_chars: list[str] = []
    offsets: list[int] = []
    prev_space = True
    for i, ch in enumerate(turn_text):
        if ch.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            offsets.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            offsets.append(i)
            prev_space = False
    norm_text = "".join(norm_chars).strip()
    # `strip()` above can only remove a trailing space we appended, so the
    # offset list stays aligned for every index we look up below.
    n_idx = norm_text.find(norm_span)
    if n_idx == -1 or n_idx + len(norm_span) > len(offsets):
        return None
    start = offsets[n_idx]
    end_norm_idx = n_idx + len(norm_span) - 1
    end = offsets[end_norm_idx] + 1
    return start, end


def validate_thought_construction(
    raw_content: str, *, turn_texts: dict[int, str]
) -> ValidationResult:
    """`turn_texts` maps turn_index to the exact text the prompt showed the
    model, which is what `text_span` must be a substring of.
    """
    parsed, err = parse_json_object(raw_content)
    if parsed is None:
        return ValidationResult(valid=False, error=err)

    try:
        jsonschema.validate(parsed, THOUGHT_CONSTRUCTION_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ValidationResult(valid=False, error=f"schema violation at {path}: {exc.message}")

    thoughts: list[dict[str, object]] = parsed["thoughts"]  # type: ignore[assignment]
    for i, thought in enumerate(thoughts):
        turn_index = int(thought["turn_index"])  # type: ignore[call-overload]
        if turn_index not in turn_texts:
            known = ", ".join(str(t) for t in sorted(turn_texts))
            return ValidationResult(
                valid=False,
                error=(
                    f"thoughts[{i}].turn_index = {turn_index} does not exist in this "
                    f"transcript; valid turn_index values are: {known}"
                ),
            )
        span = str(thought["text_span"])
        if locate_span(turn_texts[turn_index], span) is None:
            return ValidationResult(
                valid=False,
                error=(
                    f"thoughts[{i}].text_span is not a verbatim substring of turn "
                    f"{turn_index}. The span must be copied character-for-character "
                    f"from the turn text, including any disfluency or repetition. "
                    f"You wrote: {span!r}. Turn {turn_index} reads: "
                    f"{turn_texts[turn_index]!r}"
                ),
            )

    return ValidationResult(valid=True, error="", items=thoughts)


def validate_edge_prediction(raw_content: str, *, known_ids: set[str]) -> ValidationResult:
    parsed, err = parse_json_object(raw_content)
    if parsed is None:
        return ValidationResult(valid=False, error=err)

    try:
        jsonschema.validate(parsed, EDGE_PREDICTION_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ValidationResult(valid=False, error=f"schema violation at {path}: {exc.message}")

    edges: list[dict[str, object]] = parsed["edges"]  # type: ignore[assignment]
    seen: set[tuple[str, str]] = set()
    for i, edge in enumerate(edges):
        src = str(edge["src_thought_id"])
        dst = str(edge["dst_thought_id"])
        for role, tid in (("src_thought_id", src), ("dst_thought_id", dst)):
            if tid not in known_ids:
                return ValidationResult(
                    valid=False,
                    error=(
                        f"edges[{i}].{role} = {tid!r} is not one of the thought ids you "
                        f"were given. Use only ids from the thought list."
                    ),
                )
        if src == dst:
            return ValidationResult(
                valid=False,
                error=f"edges[{i}] is a self-edge on {src!r}; source and target must differ",
            )
        if (src, dst) in seen:
            return ValidationResult(
                valid=False,
                error=(
                    f"edges[{i}] duplicates an earlier edge from {src!r} to {dst!r}; "
                    f"emit at most one edge per ordered pair"
                ),
            )
        seen.add((src, dst))

    return ValidationResult(valid=True, error="", items=edges)


__all__ = [
    "CLINICAL_CATEGORIES",
    "POLARITIES",
    "LLM_EDGE_TYPES",
    "THOUGHT_CONSTRUCTION_SCHEMA",
    "EDGE_PREDICTION_SCHEMA",
    "ValidationResult",
    "parse_json_object",
    "locate_span",
    "validate_thought_construction",
    "validate_edge_prediction",
]
