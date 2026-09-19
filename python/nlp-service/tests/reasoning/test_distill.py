"""Module 6 — hierarchical distillation into the 8 fixed fields plus summary.

Distillation is where a hallucination becomes checkable or stops being
checkable, so most of what is pinned here is **provenance**: every populated
field must carry the transcript turns its evidence came from, resolved from
the selected candidate's cited thought ids. A value with no turn behind it is
indistinguishable from an invention.

The second theme is that absence stays absent. claude_context.md §3: absent
information is null, never a guess — so an empty candidate must leave the
field unset rather than emit a present-but-empty record that later reads as
"the model looked and found nothing".
"""

from __future__ import annotations

import pytest
from factories import Cat, candidate, thought

from coda.v1 import clinical_pb2, got_pb2
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning.distill import (
    FIELD_KEYS,
    distill_note,
    generate_summary,
    note_to_json,
    render_note_for_summary,
    resolve_turn_ids,
)
from nlp_service.reasoning.refine import FieldOutcome


def outcome(field_key: str, cand: got_pb2.Candidate, *, confidence: float = 0.8) -> FieldOutcome:
    return FieldOutcome(
        field_key=field_key,
        trace=got_pb2.RefinementTrace(),
        selected=cand,
        score=got_pb2.ScoreBreakdown(aggregate=0.6),
        confidence=confidence,
    )


def thoughts() -> list:
    return [
        thought("t1", 3, "chest pain for two days", ["chest pain"]),
        thought("t2", 7, "worse on exertion", ["exertion"]),
        thought(
            "t3", 1, "diabetes for ten years", ["diabetes"], category=Cat.THOUGHT_CATEGORY_HISTORY
        ),
        thought(
            "t4", 5, "penicillin allergy", ["penicillin"], category=Cat.THOUGHT_CATEGORY_ALLERGY
        ),
    ]


# ----------------------------------------------------------------- provenance


def test_turn_ids_are_deduplicated_and_sorted() -> None:
    """Two thoughts from one turn cite that turn once, and the list reads in
    transcript order so a reviewer can follow it against the recording."""
    by_id = {t.id: t for t in thoughts()}
    by_id["t5"] = thought("t5", 3, "and it is sharp", ["sharp"])
    assert resolve_turn_ids(["t2", "t1", "t5"], by_id) == [3, 7]


def test_an_unknown_thought_id_loses_one_entry_not_the_field() -> None:
    """Validation already rejected unknown ids at generation time, so one
    arriving here means a thought was dropped between graph build and
    distillation. Losing a provenance entry beats losing the value."""
    by_id = {t.id: t for t in thoughts()}
    assert resolve_turn_ids(["t1", "t-vanished"], by_id) == [3]


def test_every_populated_field_carries_its_turns() -> None:
    """claude_context.md §3's `{ value, source_turn_ids[], confidence }` on
    every field, which is what makes an unsupported value visible."""
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint",
                candidate("chest pain, worse on exertion", source_thought_ids=["t1", "t2"]),
            ),
            "past_medical_history": outcome(
                "past_medical_history",
                candidate("diabetes", items=["diabetes"], source_thought_ids=["t3"]),
            ),
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    assert note.chief_complaint.value == "chest pain, worse on exertion"
    assert list(note.chief_complaint.source_turn_ids) == [3, 7]
    assert note.chief_complaint.confidence == pytest.approx(0.8)
    assert list(note.past_medical_history[0].source_turn_ids) == [1]


def test_distillation_writes_turn_ids_back_onto_the_outcome() -> None:
    """The pipeline builds each field's trace `final_value` from
    `outcome.turn_ids`, which only distillation can resolve. If this ever
    stopped writing back, every persisted trace would silently claim no
    provenance while the note itself looked fine.
    """
    oc = outcome("chief_complaint", candidate("chest pain", source_thought_ids=["t1", "t2"]))
    assert oc.turn_ids == []
    distill_note(
        {"chief_complaint": oc}, thoughts=thoughts(), consultation_id="c1", run_config_id="rc1"
    )
    assert oc.turn_ids == [3, 7]


# ------------------------------------------------------------ field placement


def test_list_fields_land_in_their_proto_homes() -> None:
    """medications and allergies are two candidate field keys but one proto
    message. Getting this mapping wrong would file every allergy as a drug."""
    note = distill_note(
        {
            "medications": outcome(
                "medications",
                candidate("metformin", items=["metformin 500mg BD"], source_thought_ids=["t3"]),
            ),
            "allergies": outcome(
                "allergies",
                candidate("penicillin", items=["penicillin"], source_thought_ids=["t4"]),
            ),
            "provisional_diagnosis": outcome(
                "provisional_diagnosis",
                candidate("angina", items=["stable angina"], source_thought_ids=["t1"]),
            ),
            "investigations_advised": outcome(
                "investigations_advised",
                candidate("ECG", items=["ECG", "troponin"], source_thought_ids=["t1"]),
            ),
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    assert [v.value for v in note.medications_allergies.medications] == ["metformin 500mg BD"]
    assert [v.value for v in note.medications_allergies.allergies] == ["penicillin"]
    assert [v.value for v in note.provisional_diagnosis] == ["stable angina"]
    assert [v.value for v in note.investigations_advised] == ["ECG", "troponin"]


def test_scalar_fields_land_in_their_proto_homes() -> None:
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("cc", source_thought_ids=["t1"])
            ),
            "hopi": outcome("hopi", candidate("hopi text", source_thought_ids=["t1"])),
            "examination_findings": outcome(
                "examination_findings", candidate("exam text", source_thought_ids=["t1"])
            ),
            "treatment_plan": outcome(
                "treatment_plan", candidate("plan text", source_thought_ids=["t1"])
            ),
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    assert note.chief_complaint.value == "cc"
    assert note.hopi.value == "hopi text"
    assert note.examination_findings.value == "exam text"
    assert note.treatment_plan.value == "plan text"


def test_the_field_key_list_matches_the_retrieval_mapping() -> None:
    """Taken from `graph.retrieval.FIELD_SEED_CATEGORIES` rather than
    re-declared, so a thought can never be retrieved for one field and
    distilled into another."""
    from nlp_service.graph.retrieval import FIELD_SEED_CATEGORIES

    assert tuple(FIELD_SEED_CATEGORIES) == FIELD_KEYS
    assert len(FIELD_KEYS) == 9, "8 clinical fields, with medications/allergies split"


# --------------------------------------------------------------- absence


def test_an_empty_candidate_leaves_the_field_unset() -> None:
    """Not an empty string, not a present-but-blank record — unset, so the
    eval scores it as absent rather than as a wrong answer."""
    note = distill_note(
        {
            "chief_complaint": outcome("chief_complaint", candidate("   ")),
            "past_medical_history": outcome("past_medical_history", candidate("", items=[])),
            "allergies": outcome("allergies", candidate("", items=["  ", ""])),
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    assert not note.HasField("chief_complaint")
    assert len(note.past_medical_history) == 0
    assert len(note.medications_allergies.allergies) == 0


def test_a_field_with_no_outcome_at_all_is_simply_absent() -> None:
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("cc", source_thought_ids=["t1"])
            )
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    assert note.HasField("chief_complaint")
    assert not note.HasField("hopi")
    assert not note.HasField("treatment_plan")
    assert note.status == clinical_pb2.NoteStatus.NOTE_STATUS_DRAFT
    assert note.version == 1


# --------------------------------------------------------------- the summary


def test_the_summary_prompt_hides_metadata_from_the_model() -> None:
    """Turn ids and confidences invite the model to write about the pipeline
    instead of the consultation."""
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("chest pain", source_thought_ids=["t1"])
            ),
            "allergies": outcome(
                "allergies",
                candidate("penicillin", items=["penicillin"], source_thought_ids=["t4"]),
            ),
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    rendered = render_note_for_summary(note)
    assert "Chief complaint: chest pain" in rendered
    assert "Allergies: penicillin" in rendered
    assert "source_turn_ids" not in rendered
    assert "confidence" not in rendered
    assert "0.8" not in rendered


async def test_an_empty_note_costs_no_summary_call(fake_conn) -> None:
    """Spending a call to have a model say there was nothing would cost quota
    and invite it to invent a consultation."""
    client = CassetteLLMClient(turns=[])
    result = await generate_summary(
        conn=fake_conn,
        llm_client=client,
        model="m",
        note=clinical_pb2.ClinicalNote(consultation_id="c1"),
        temperature=0.0,
        timeout_s=5.0,
    )
    assert result.text == ""
    assert result.llm_calls == 0
    assert client.calls == []


async def test_a_too_short_summary_is_recorded_as_missing(fake_conn) -> None:
    """Scored as absent rather than as a bad attempt — a two-word fragment is
    not a summary, and letting it through would flatter the summarization
    metrics rather than reporting the failure."""
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("chest pain", source_thought_ids=["t1"])
            )
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    client = CassetteLLMClient(turns=[CassetteTurn(content="ok")])
    result = await generate_summary(
        conn=fake_conn,
        llm_client=client,
        model="m",
        note=note,
        temperature=0.0,
        timeout_s=5.0,
    )
    assert result.text == ""
    assert result.llm_calls == 1, "the call was spent and must still be accounted for"


async def test_a_real_summary_is_kept_with_its_token_accounting(fake_conn) -> None:
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("chest pain", source_thought_ids=["t1"])
            )
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(
                content="The patient presented with two days of chest pain, worse on exertion.",
                tokens_in=120,
                tokens_out=30,
            )
        ]
    )
    result = await generate_summary(
        conn=fake_conn,
        llm_client=client,
        model="m",
        note=note,
        temperature=0.0,
        timeout_s=5.0,
    )
    assert result.text.startswith("The patient presented")
    assert (result.tokens_in, result.tokens_out, result.llm_calls) == (120, 30, 1)


def test_the_note_artifact_uses_snake_case_field_names() -> None:
    """Every other artifact this system writes uses
    `preserving_proto_field_name`, and one reader handles them all."""
    note = distill_note(
        {
            "chief_complaint": outcome(
                "chief_complaint", candidate("cc", source_thought_ids=["t1"])
            )
        },
        thoughts=thoughts(),
        consultation_id="c1",
        run_config_id="rc1",
    )
    raw = note_to_json(note)
    assert "source_turn_ids" in raw
    assert "sourceTurnIds" not in raw
    assert "run_config_id" in raw
