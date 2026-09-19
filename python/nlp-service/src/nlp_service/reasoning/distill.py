"""Module 6 — hierarchical distillation into the 8 fixed clinical fields plus
the consultation summary.

The paper learns a soft clustering into 5 clinical themes. We do not, and the
reason is not cost — it is that the problem does not exist here.
claude_context.md §6: "The field taxonomy is already fixed and smaller than
the paper's clusters; learned clustering would solve a problem we do not
have." The 8 fields (§3) are given, and the mapping from a thought's
`clinical_category` to its field is a rule, stated once in
`graph.retrieval.FIELD_SEED_CATEGORIES` and reused here rather than restated
— the same mapping that seeds retrieval also determines distillation, so a
thought can never be retrieved for one field and distilled into another.

## Provenance is the load-bearing part

Every field of the output carries `source_turn_ids`, resolved from the
selected candidate's cited thought ids back to the transcript turns those
thoughts came from. This is claude_context.md §3's requirement
(`{ value, source_turn_ids[], confidence }`) and it is what makes a
hallucination checkable: a value citing no turn, or citing a turn that says
something else, is visible rather than merely suspected.

The resolution is thought_id -> Thought.turn_index, deduplicated and sorted.
It is done here, at distillation, rather than during generation, because only
here is the final selected candidate known — an intermediate candidate's
citations describe a value that did not survive.

## The summary

Written from the distilled note, not from the transcript. The baseline arm
summarizes raw turns (`nlp_service.summary`); the GoT arm summarizes its own
structured output, so the summary inherits the note's polarity handling and
cannot reintroduce a finding the note ruled out. That is a deliberate
asymmetry between the arms and it is a property of the method being tested,
not an inconsistency: the GoT arm's whole claim is that the structured
intermediate representation is worth having.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from google.protobuf import json_format

from coda.v1 import clinical_pb2, thought_pb2
from nlp_service import prompts
from nlp_service.graph.retrieval import FIELD_SEED_CATEGORIES
from nlp_service.llm.client import LLMClient
from nlp_service.llm_cache import complete_cached
from nlp_service.reasoning.generation import LIST_FIELDS
from nlp_service.reasoning.refine import FieldOutcome

logger = logging.getLogger(__name__)

FIELD_KEYS: tuple[str, ...] = tuple(FIELD_SEED_CATEGORIES)
"""The 8 clinical fields with medications/allergies split into two keys —
identical to `nlp_service.db.FIELD_KEYS` and migration 000019's CHECK. Taken
from the retrieval mapping rather than re-declared, so the two can never
drift apart."""


def resolve_turn_ids(
    source_thought_ids: list[str], thoughts_by_id: dict[str, thought_pb2.Thought]
) -> list[int]:
    """Cited thought ids -> the transcript turns those thoughts came from.

    Silently skips an unknown id rather than failing: validation already
    rejected unknown ids at generation time, so one reaching here means a
    thought was dropped between graph build and distillation, and losing one
    provenance entry is better than losing the whole field.
    """
    turns = {
        thoughts_by_id[tid].turn_index for tid in source_thought_ids if tid in thoughts_by_id
    }
    return sorted(turns)


def _field_value(
    text: str, *, turn_ids: list[int], confidence: float
) -> clinical_pb2.FieldValue:
    return clinical_pb2.FieldValue(
        value=text, source_turn_ids=turn_ids, confidence=confidence
    )


def distill_note(
    outcomes: dict[str, FieldOutcome],
    *,
    thoughts: list[thought_pb2.Thought],
    consultation_id: str,
    run_config_id: str,
) -> clinical_pb2.ClinicalNote:
    """Rule-based assembly of the selected per-field candidates into the
    8-field note, with turn-id provenance on every populated field."""
    by_id = {t.id: t for t in thoughts}
    note = clinical_pb2.ClinicalNote(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        version=1,
        status=clinical_pb2.NoteStatus.NOTE_STATUS_DRAFT,
    )

    for field_key in FIELD_KEYS:
        outcome = outcomes.get(field_key)
        if outcome is None:
            continue
        turn_ids = resolve_turn_ids(list(outcome.selected.source_thought_ids), by_id)
        outcome.turn_ids = turn_ids
        confidence = outcome.confidence

        if field_key in LIST_FIELDS:
            values = [
                _field_value(item, turn_ids=turn_ids, confidence=confidence)
                for item in outcome.selected.items
                if item.strip()
            ]
            if not values:
                # §3: absent information is null/empty, never a fabricated
                # empty-but-present record.
                continue
            if field_key == "past_medical_history":
                note.past_medical_history.extend(values)
            elif field_key == "medications":
                note.medications_allergies.medications.extend(values)
            elif field_key == "allergies":
                note.medications_allergies.allergies.extend(values)
            elif field_key == "provisional_diagnosis":
                note.provisional_diagnosis.extend(values)
            elif field_key == "investigations_advised":
                note.investigations_advised.extend(values)
            continue

        text = outcome.selected.text.strip()
        if not text:
            continue
        value = _field_value(text, turn_ids=turn_ids, confidence=confidence)
        if field_key == "chief_complaint":
            note.chief_complaint.CopyFrom(value)
        elif field_key == "hopi":
            note.hopi.CopyFrom(value)
        elif field_key == "examination_findings":
            note.examination_findings.CopyFrom(value)
        elif field_key == "treatment_plan":
            note.treatment_plan.CopyFrom(value)

    def _has_content(fk: str) -> bool:
        oc = outcomes.get(fk)
        return bool(oc and (oc.selected.text.strip() or oc.selected.items))

    populated = sum(1 for fk in FIELD_KEYS if _has_content(fk))
    logger.info(
        "distillation complete",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "populated_fields": populated,
                "empty_fields": len(FIELD_KEYS) - populated,
            }
        },
    )
    return note


def render_note_for_summary(note: clinical_pb2.ClinicalNote) -> str:
    """The note as the summary prompt sees it. Plain labelled text rather
    than the raw proto JSON: the summary model does not need field numbers,
    confidences or turn ids, and sending them invites it to write about the
    metadata."""
    lines: list[str] = []

    def scalar(label: str, fv: clinical_pb2.FieldValue, present: bool) -> None:
        if present and fv.value.strip():
            lines.append(f"{label}: {fv.value.strip()}")

    def listed(label: str, values: list[clinical_pb2.FieldValue]) -> None:
        texts = [v.value.strip() for v in values if v.value.strip()]
        if texts:
            lines.append(f"{label}: " + "; ".join(texts))

    scalar("Chief complaint", note.chief_complaint, note.HasField("chief_complaint"))
    scalar("History of present illness", note.hopi, note.HasField("hopi"))
    listed("Past medical history", list(note.past_medical_history))
    listed("Medications", list(note.medications_allergies.medications))
    listed("Allergies", list(note.medications_allergies.allergies))
    scalar(
        "Examination findings",
        note.examination_findings,
        note.HasField("examination_findings"),
    )
    listed("Provisional diagnosis", list(note.provisional_diagnosis))
    listed("Investigations advised", list(note.investigations_advised))
    scalar("Treatment plan", note.treatment_plan, note.HasField("treatment_plan"))

    return "\n".join(lines) if lines else "(the note is empty — no field was populated)"


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cache_hits: int = 0


async def generate_summary(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    note: clinical_pb2.ClinicalNote,
    temperature: float,
    timeout_s: float,
    min_chars: int = 20,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> SummaryResult:
    rendered = render_note_for_summary(note)
    if rendered.startswith("(the note is empty"):
        # Nothing to summarize. Spending a call to have a model say so would
        # cost quota and risk it inventing a consultation.
        logger.warning(
            "skipping summary generation: the distilled note has no populated field",
            extra={"extra_fields": {"consultation_id": note.consultation_id}},
        )
        return SummaryResult(text="")

    pr = prompts.load_got_summary_prompts(language=language)
    completion, cache_hit = await complete_cached(
        conn,
        llm_client,
        model=model,
        system_prompt=pr.system,
        user_prompt=pr.user_template.format(note=rendered),
        temperature=temperature,
        timeout_s=timeout_s,
        json_mode=False,
    )
    text = completion.content.strip()
    if len(text) < min_chars:
        # Not fatal: a note exists and is the primary output. A too-short
        # summary is recorded as the empty string so the eval scores it as
        # missing rather than scoring a fragment as if it were an attempt.
        logger.warning(
            "summary was shorter than the sanity floor; recording it as empty",
            extra={
                "extra_fields": {
                    "consultation_id": note.consultation_id,
                    "length": len(text),
                    "min_chars": min_chars,
                }
            },
        )
        text = ""
    return SummaryResult(
        text=text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
        llm_calls=1,
        cache_hits=1 if cache_hit else 0,
    )


def note_to_json(note: clinical_pb2.ClinicalNote) -> str:
    return json_format.MessageToJson(note, preserving_proto_field_name=True, indent=None)


__all__ = [
    "FIELD_KEYS",
    "SummaryResult",
    "resolve_turn_ids",
    "distill_note",
    "render_note_for_summary",
    "generate_summary",
    "note_to_json",
]
