"""GoT-HCS Module 1 against the four hand-built dialogue fixtures.

Each test names the dialogue phenomenon it isolates (claude_context.md's
hypothesis H3: thought construction from dialogue is a materially different
node-construction problem from thought construction over prose EHR text).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fixtures import transcripts

from coda.v1 import thought_pb2, transcript_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service.graph import schema
from nlp_service.graph.thoughts import construct_thoughts

CASSETTES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

Pol = thought_pb2.Polarity
Cat = thought_pb2.ThoughtCategory


def cassette(name: str, *, only_construction: bool = True):
    from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

    data = json.loads((CASSETTES / f"{name}.json").read_text())
    if only_construction:
        data = data[:1]
    return CassetteLLMClient(turns=[CassetteTurn(**t) for t in data])


def turn_ids(transcript: transcript_pb2.Transcript) -> dict[int, str]:
    """Stand-in for what `store.materialize_transcript` returns."""
    return {t.turn_index: f"uuid-turn-{t.turn_index}" for t in transcript.turns}


async def build(name: str, transcript: transcript_pb2.Transcript, conn: object):
    return await construct_thoughts(
        conn=conn,  # type: ignore[arg-type]
        llm_client=cassette(name),
        model="qwen/qwen3.8-27b",
        transcript=transcript,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        turn_id_by_index=turn_ids(transcript),
        repair_max_attempts=2,
        timeout_s=5.0,
    )


# --------------------------------------------------------------------------
# Case 1 — cross-turn negation
# --------------------------------------------------------------------------


async def test_negation_produces_two_linked_thoughts_not_one(fake_conn: object) -> None:
    """The whole point of modelling polarity: a symptom asserted by the
    patient in turn 3 and negated by the doctor in turn 9 must survive as
    TWO thoughts. A system that resolves the contradiction during
    construction destroys the evidence the graph exists to reason over.
    """
    transcript = transcripts.negation_transcript()
    result = await build("negation", transcript, fake_conn)

    penicillin = [
        t
        for t in result.thoughts
        if any("penicillin" in e.text.lower() for e in t.entities)
        and t.category == Cat.THOUGHT_CATEGORY_ALLERGY
    ]
    assert len(penicillin) == 2, "expected the asserted and the negated allergy to both survive"

    asserted = [t for t in penicillin if t.polarity == Pol.POLARITY_ASSERTED]
    negated = [t for t in penicillin if t.polarity == Pol.POLARITY_NEGATED]
    assert len(asserted) == 1
    assert len(negated) == 1

    # Non-adjacent, and by different speakers — which is what makes this
    # unrecoverable by any within-turn or adjacent-turn heuristic.
    assert asserted[0].turn_index == 3
    assert negated[0].turn_index == 9
    assert negated[0].turn_index - asserted[0].turn_index == 6
    assert asserted[0].speaker == transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT
    assert negated[0].speaker == transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR


async def test_speaker_comes_from_the_transcript_not_the_model(fake_conn: object) -> None:
    """Speaker is the diarizer's answer, never re-derived by the model —
    doctor-negates-patient is exactly the distinction that must not be
    guessable.
    """
    transcript = transcripts.negation_transcript()
    result = await build("negation", transcript, fake_conn)
    expected = {t.turn_index: t.speaker_label for t in transcript.turns}
    for thought in result.thoughts:
        assert thought.speaker == expected[thought.turn_index]


# --------------------------------------------------------------------------
# Case 2 — interruption
# --------------------------------------------------------------------------


async def test_interrupted_thought_is_completed_but_stays_anchored_to_its_own_turn(
    fake_conn: object,
) -> None:
    """Turn 2 is cut off ("and it goes"); turn 4 completes it. The completed
    fact must be anchored to turn 4 — the turn where the content was said —
    not merged backwards into turn 2, or provenance points at words that
    never carried the claim.
    """
    transcript = transcripts.interruption_transcript()
    result = await build("interruption", transcript, fake_conn)

    radiating = [t for t in result.thoughts if "left arm" in t.text.lower()]
    assert len(radiating) == 1
    thought = radiating[0]

    assert thought.turn_index == 4
    # `text` completes the interrupted sentence...
    assert "chest pain" in thought.text.lower()
    # ...while `text_span` stays verbatim inside turn 4 alone.
    turn_4 = next(t for t in transcript.turns if t.turn_index == 4).text_redacted
    span = turn_4[thought.char_start : thought.char_end]
    assert span == "Down my left arm"
    assert "chest" not in span.lower()


async def test_clarifying_question_turn_yields_no_thought(fake_conn: object) -> None:
    """The doctor's interruption in turn 3 ("Sorry — goes where?") states no
    clinical fact. Emitting a thought for it would put a question into the
    graph as if it were evidence.
    """
    transcript = transcripts.interruption_transcript()
    result = await build("interruption", transcript, fake_conn)
    assert 3 not in {t.turn_index for t in result.thoughts}


# --------------------------------------------------------------------------
# Case 3 — disfluency
# --------------------------------------------------------------------------


async def test_disfluent_span_is_verbatim_while_text_is_cleaned(fake_conn: object) -> None:
    """`text_span` is provenance and must survive character-for-character,
    disfluency included; `text` is the cleaned assertion. Conflating the two
    would either corrupt provenance or push filler into the note.
    """
    transcript = transcripts.disfluency_transcript()
    result = await build("disfluency", transcript, fake_conn)
    texts = {t.turn_index: t.text_redacted for t in transcript.turns}

    for thought in result.thoughts:
        span = texts[thought.turn_index][thought.char_start : thought.char_end]
        assert span == texts[thought.turn_index][thought.char_start : thought.char_end]
        assert span in texts[thought.turn_index]

    headache = next(t for t in result.thoughts if t.turn_index == 1)
    raw_span = texts[1][headache.char_start : headache.char_end]
    assert "uh," in raw_span, "the stored span must keep the disfluency it was drawn from"
    assert "uh," not in headache.text, "the cleaned assertion must not carry filler"
    assert "headache" in headache.text.lower()


async def test_self_repair_takes_the_repaired_value(fake_conn: object) -> None:
    """Turn 5 is "Since, uh, since Tuesday, no, Monday. It was Monday." The
    temporal anchor must be Monday. Taking the first-stated value would put a
    wrong onset date into the note from a turn that explicitly corrects it.
    """
    transcript = transcripts.disfluency_transcript()
    result = await build("disfluency", transcript, fake_conn)
    onset = next(t for t in result.thoughts if t.turn_index == 5)
    assert onset.temporal_anchor == "Monday"
    assert "Tuesday" not in onset.text


async def test_backchannel_turn_yields_no_thought(fake_conn: object) -> None:
    """Turn 2 is "Mm-hmm." — pure backchannel, no clinical content."""
    transcript = transcripts.disfluency_transcript()
    result = await build("disfluency", transcript, fake_conn)
    assert 2 not in {t.turn_index for t in result.thoughts}


# --------------------------------------------------------------------------
# Case 4 — one symptom across three non-adjacent turns
# --------------------------------------------------------------------------


async def test_symptom_across_three_non_adjacent_turns_yields_three_thoughts(
    fake_conn: object,
) -> None:
    """The cough is introduced in turn 1, dated in turn 5, characterized in
    turn 9, with unrelated exchanges between. Each fragment is its own
    thought at its own turn; assembling them is the graph's job, not
    construction's.
    """
    transcript = transcripts.cross_turn_symptom_transcript()
    result = await build("cross_turn_symptom", transcript, fake_conn)

    cough = [t for t in result.thoughts if any(e.text.lower() == "cough" for e in t.entities)]
    assert {t.turn_index for t in cough} == {1, 5, 9}

    # Non-adjacent by construction — no two of them are consecutive turns.
    indices = sorted(t.turn_index for t in cough)
    assert all(b - a > 1 for a, b in zip(indices, indices[1:], strict=False))

    # Each fragment carries only what its own turn said.
    by_turn = {t.turn_index: t for t in cough}
    assert by_turn[5].temporal_anchor == "about three weeks"
    assert not by_turn[1].temporal_anchor
    assert "night" in by_turn[9].text.lower()


async def test_negated_history_is_recorded_not_dropped(fake_conn: object) -> None:
    """ "No, nothing like that" in reply to a travel question is a clinically
    meaningful negative. A construction step that only records positives
    silently loses every pertinent negative in the consultation.
    """
    transcript = transcripts.cross_turn_symptom_transcript()
    result = await build("cross_turn_symptom", transcript, fake_conn)
    travel = next(t for t in result.thoughts if t.turn_index == 3)
    assert travel.polarity == Pol.POLARITY_NEGATED
    assert travel.category == Cat.THOUGHT_CATEGORY_HISTORY


# --------------------------------------------------------------------------
# Structural guarantees across every fixture
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("negation", transcripts.negation_transcript),
        ("interruption", transcripts.interruption_transcript),
        ("disfluency", transcripts.disfluency_transcript),
        ("cross_turn_symptom", transcripts.cross_turn_symptom_transcript),
    ],
)
async def test_every_thought_is_fully_provenanced(
    name: str, factory: object, fake_conn: object
) -> None:
    """The tuple this project committed to: (thought_id, speaker, text_span,
    turn_id, entities, clinical_category, temporal_anchor, polarity,
    confidence). Every one populated, and the span actually indexing the
    turn it claims.
    """
    transcript = factory()  # type: ignore[operator]
    result = await build(name, transcript, fake_conn)
    texts = {t.turn_index: t.text_redacted for t in transcript.turns}
    ids = set()

    assert result.thoughts
    assert not result.dropped
    for thought in result.thoughts:
        assert thought.id and thought.id not in ids
        ids.add(thought.id)
        assert thought.turn_id == f"uuid-turn-{thought.turn_index}"
        assert thought.speaker != transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNSPECIFIED
        assert thought.text.strip()
        assert thought.category != Cat.THOUGHT_CATEGORY_UNSPECIFIED
        assert thought.polarity != Pol.POLARITY_UNSPECIFIED
        assert 0.0 <= thought.confidence <= 1.0
        assert thought.char_end > thought.char_start
        assert thought.char_end <= len(texts[thought.turn_index])
        assert texts[thought.turn_index][thought.char_start : thought.char_end]


async def test_thought_citing_an_unpersisted_turn_is_dropped_not_dangling(
    fake_conn: object,
) -> None:
    """A thought whose turn has no database row would be a dangling foreign
    key. It is dropped and reported, never written.
    """
    transcript = transcripts.negation_transcript()
    partial = {i: f"uuid-turn-{i}" for i in (1, 3, 5, 7, 10)}  # turn 9 missing
    result = await construct_thoughts(
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=cassette("negation"),
        model="qwen/qwen3.8-27b",
        transcript=transcript,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        turn_id_by_index=partial,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert 9 not in {t.turn_index for t in result.thoughts}
    assert any("turn_index 9" in reason for reason in result.dropped)


# --------------------------------------------------------------------------
# The repair loop
# --------------------------------------------------------------------------


async def test_non_verbatim_span_is_rejected_into_the_repair_loop(fake_conn: object) -> None:
    """A paraphrased span is a structurally perfect object that breaks the
    provenance claim, so it must fail validation rather than be stored. The
    repair addendum has to name the specific span.
    """
    from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

    transcript = transcripts.disfluency_transcript()
    bad = {
        "thoughts": [
            {
                "turn_index": 1,
                "text_span": "I have been getting headaches at the back of my head",
                "text": "Headaches at the back of the head",
                "entities": ["headache"],
                "clinical_category": "symptom",
                "temporal_anchor": None,
                "polarity": "asserted",
                "confidence": 0.9,
            }
        ]
    }
    good = json.loads((CASSETTES / "disfluency.json").read_text())[0]
    client = CassetteLLMClient(turns=[CassetteTurn(content=json.dumps(bad)), CassetteTurn(**good)])

    result = await construct_thoughts(
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript=transcript,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        turn_id_by_index=turn_ids(transcript),
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert result.repair_attempts == 1
    assert result.thoughts
    repair_prompt = str(client.calls[1]["user_prompt"])
    assert "not a verbatim substring of turn 1" in repair_prompt


async def test_exhausted_repair_budget_fails_loudly(fake_conn: object) -> None:
    """Never guess a graph into place — the same discipline extraction.py
    applies to the 8-field note.
    """
    from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

    transcript = transcripts.disfluency_transcript()
    junk = CassetteTurn(content='{"thoughts": [{"turn_index": 99}]}')
    client = CassetteLLMClient(turns=[junk, junk])

    with pytest.raises(FatalError) as exc:
        await construct_thoughts(
            conn=fake_conn,  # type: ignore[arg-type]
            llm_client=client,
            model="qwen/qwen3.8-27b",
            transcript=transcript,
            consultation_id="c-fixture",
            run_config_id="rc-fixture",
            turn_id_by_index=turn_ids(transcript),
            repair_max_attempts=1,
            timeout_s=5.0,
        )
    assert exc.value.code == "THOUGHT_CONSTRUCTION_INVALID"


def test_span_location_tolerates_whitespace_rewrapping() -> None:
    """A model that re-wraps a copied span has not broken provenance; the
    offsets returned must still index the original string.
    """
    turn = "It's a  sort of\npressure, and it goes"
    located = schema.locate_span(turn, "It's a sort of pressure")
    assert located is not None
    start, end = located
    assert turn[start:end] == "It's a  sort of\npressure"
    assert schema.locate_span(turn, "crushing pain") is None
